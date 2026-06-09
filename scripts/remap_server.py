#!/usr/bin/env python3
"""
LEDs One — remap_server.py
Out-of-Stock SKU Remap Suggestions Module.

Accessed via: http://192.168.18.94:8080/remap
API:          http://192.168.18.94:8080/api/remap-suggestions?location=UK

This file is imported by dashboard_server.py — it does NOT run standalone.
It uses the same PostgreSQL connection (stock_level db) as the main dashboard.

Logic:
  1. Find SKUs with stock <= 5 per selected location (UK / US / Germany)
  2. Find their parent_sku from amazon_variants table
  3. Find sibling SKUs under same parent_sku with stock > 10 in that location
  4. Return best alternative (highest stock sibling)

Thresholds:
  OUT_OF_STOCK_THRESHOLD = 5   (stock <= 5 = out of stock risk)
  MIN_ALT_STOCK          = 10  (alternative must have stock > 10)

Tables used (all in PostgreSQL stock_level db):
  - location_wise_inv_stock  (sku, location, stock)
  - amazon_variants          (mapped_sku, parent_sku, asin, color)
  - ph_mapping               (mapped_sku, holder_name)
  - ebay_products            (mapped_sku, title)

Synced every 20 min by db_sync.py via OpenClaw cron.
"""

import os
import re
import json as _json
from datetime import datetime
import psycopg2
import psycopg2.extras
import yaml
from flask import request, jsonify

# ── Config path ───────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get(
    "DASHBOARD_CONFIG",
    "/opt/openclaw/stock_level/config/stock_dashboard.yaml"
)


def _load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    return {
        "postgres": {
            "host":     "localhost",
            "port":     5432,
            "dbname":   "stock_level",
            "user":     "digit_web",
            "password": "digit123",
        }
    }

def _pg_conn():
    cfg = _load_config()
    pg  = cfg["postgres"]
    args = {
        "host":   pg.get("host",   "localhost"),
        "port":   pg.get("port",   5432),
        "dbname": pg.get("dbname", "stock_level"),
        "user":   pg.get("user",   "digit_web"),
    }
    if pg.get("password"):
        args["password"] = pg["password"]
    return psycopg2.connect(**args)

# ── Thresholds — aligned with dashboard_server.py compute_status() ───────────
# Dashboard logic:
#   avg = 0                    → NO DATA  (not shown in remap — no velocity data)
#   days <= CRIT (7)           → CRITICAL → SHOW IN REMAP (first priority)
#   days <= LOW  (21)          → LOW      → NOT shown in remap (business decision)
#   days <= OVER (90)          → HEALTHY  → not shown
#   days > OVER                → OVERSTOCKED → not shown
#
# Special case: stock = 0 AND avg = 0 → NO DATA but genuinely out of stock
#   These MUST appear in remap (stock is zero, no sales history)
#
# MIN_ALT_STOCK: alternative must have stock > 10 in the same location
OUT_OF_STOCK_THRESHOLD = 5   # stock <= 5 treated as out-of-stock risk
MIN_ALT_STOCK          = 10  # alternative must have stock > 10

# ── Remap CTE Query — uses same velocity waterfall as dashboard_server.py ─────
# OOS criteria (matching dashboard CRITICAL logic):
#   CASE 1: stock = 0 (regardless of velocity — genuinely empty)
#   CASE 2: avg > 0 AND (stock / avg) <= 7 days remaining (CRITICAL)
#
# Velocity waterfall (exact match with compute_velocity in dashboard_server.py):
#   if sold_7d  >= 7 → avg = sold_7d  / 7
#   if sold_14d >= 7 → avg = sold_14d / 14
#   if sold_30d >= 7 → avg = sold_30d / 30
#   else             → avg = 0  (NO DATA — only include if stock = 0)
REMAP_QUERY = """
WITH
-- Step 1: Stock per SKU for the selected location only
-- TRIM handles MySQL CHAR padding (e.g. "IMWW " → "IMWW")
loc_stock AS (
    SELECT TRIM(sku)  AS sku,
           SUM(stock) AS total_stock
    FROM   location_wise_inv_stock
    WHERE  location = %(location)s
    GROUP  BY TRIM(sku)
),

-- Step 2: Sales velocity per SKU — exact same waterfall as dashboard_server.py
-- sold_7d/14d/30d from the last 30 days of orders
velocity AS (
    SELECT
        TRIM(oii.sku)                                               AS sku,
        COALESCE(SUM(CASE WHEN o.order_date >= CURRENT_DATE - INTERVAL '7 days'
                          THEN oii.quantity END), 0)                AS sold_7d,
        COALESCE(SUM(CASE WHEN o.order_date >= CURRENT_DATE - INTERVAL '14 days'
                          THEN oii.quantity END), 0)                AS sold_14d,
        COALESCE(SUM(oii.quantity), 0)                              AS sold_30d
    FROM order_item_info oii
    JOIN orders o ON o.internal_id = oii.order_id
    GROUP BY TRIM(oii.sku)
),

-- Step 3: Compute avg_per_day using waterfall, then days_remaining
-- Matches compute_velocity() and compute_status() in dashboard_server.py exactly
sku_status AS (
    SELECT
        ls.sku,
        ls.total_stock                                              AS stock,
        COALESCE(v.sold_7d,  0)                                    AS sold_7d,
        COALESCE(v.sold_14d, 0)                                    AS sold_14d,
        COALESCE(v.sold_30d, 0)                                    AS sold_30d,
        -- Velocity waterfall (same as compute_velocity)
        CASE
            WHEN COALESCE(v.sold_7d,  0) >= 7 THEN ROUND(COALESCE(v.sold_7d,  0)::numeric / 7,  2)
            WHEN COALESCE(v.sold_14d, 0) >= 7 THEN ROUND(COALESCE(v.sold_14d, 0)::numeric / 14, 2)
            WHEN COALESCE(v.sold_30d, 0) >= 7 THEN ROUND(COALESCE(v.sold_30d, 0)::numeric / 30, 2)
            ELSE 0
        END                                                        AS avg_per_day,
        -- Status (same as compute_status)
        CASE
            WHEN COALESCE(v.sold_7d,0)<7 AND COALESCE(v.sold_14d,0)<7
                 AND COALESCE(v.sold_30d,0)<7 THEN 'NO DATA'
            WHEN ls.total_stock = 0 THEN 'CRITICAL'
            WHEN CASE
                    WHEN COALESCE(v.sold_7d,0) >=7 THEN ls.total_stock / (COALESCE(v.sold_7d,0)::float/7)
                    WHEN COALESCE(v.sold_14d,0)>=7 THEN ls.total_stock / (COALESCE(v.sold_14d,0)::float/14)
                    WHEN COALESCE(v.sold_30d,0)>=7 THEN ls.total_stock / (COALESCE(v.sold_30d,0)::float/30)
                    ELSE 999
                 END <= 7  THEN 'CRITICAL'
            WHEN CASE
                    WHEN COALESCE(v.sold_7d,0) >=7 THEN ls.total_stock / (COALESCE(v.sold_7d,0)::float/7)
                    WHEN COALESCE(v.sold_14d,0)>=7 THEN ls.total_stock / (COALESCE(v.sold_14d,0)::float/14)
                    WHEN COALESCE(v.sold_30d,0)>=7 THEN ls.total_stock / (COALESCE(v.sold_30d,0)::float/30)
                    ELSE 999
                 END <= 21 THEN 'LOW'
            WHEN CASE
                    WHEN COALESCE(v.sold_7d,0) >=7 THEN ls.total_stock / (COALESCE(v.sold_7d,0)::float/7)
                    WHEN COALESCE(v.sold_14d,0)>=7 THEN ls.total_stock / (COALESCE(v.sold_14d,0)::float/14)
                    WHEN COALESCE(v.sold_30d,0)>=7 THEN ls.total_stock / (COALESCE(v.sold_30d,0)::float/30)
                    ELSE 999
                 END <= 90 THEN 'HEALTHY'
            ELSE 'OVERSTOCKED'
        END                                                        AS dash_status
    FROM loc_stock ls
    LEFT JOIN velocity v ON v.sku = ls.sku
),

-- Step 4: Deduplicated amazon_variants — one row per mapped_sku
av_deduped AS (
    SELECT DISTINCT ON (mapped_sku)
           mapped_sku,
           parent_sku,
           asin,
           color
    FROM   amazon_variants
    WHERE  mapped_sku IS NOT NULL
      AND  parent_sku IS NOT NULL
      AND  mapped_sku != ''
      AND  parent_sku != ''
    ORDER  BY mapped_sku,
              (color IS NULL OR color = '') ASC,
              (asin  IS NULL OR asin  = '') ASC
),

-- Step 5: OOS SKUs — only CRITICAL status (same as dashboard definition)
-- Also includes stock=0 NO DATA products (genuinely empty, no sales history)
-- Title priority: selected location → UK → US → global (ep.title)
-- NULLIF(...,'') treats empty string same as NULL so fallback chain works correctly
oos AS (
    SELECT DISTINCT ON (ss.sku)
        ss.sku,
        ss.stock,
        ss.dash_status,
        ss.avg_per_day,
        av.parent_sku,
        av.asin,
        av.color,
        pm.holder_name,
        COALESCE(
            NULLIF(TRIM(ept_loc.title), ''),
            NULLIF(TRIM(ept_uk.title),  ''),
            NULLIF(TRIM(ept_us.title),  ''),
            ep.title
        )                       AS product_name
    FROM   sku_status           ss
    LEFT JOIN   av_deduped      av     ON av.mapped_sku  = ss.sku
    JOIN   ph_mapping           pm     ON pm.mapped_sku  = ss.sku
    LEFT JOIN ebay_products     ep     ON ep.mapped_sku  = ss.sku
    LEFT JOIN ebay_product_titles ept_loc
                                       ON ept_loc.mapped_sku = ss.sku
                                      AND ept_loc.site       = %(location)s
    LEFT JOIN ebay_product_titles ept_uk
                                       ON ept_uk.mapped_sku  = ss.sku
                                      AND ept_uk.site        = 'UK'
    LEFT JOIN ebay_product_titles ept_us
                                       ON ept_us.mapped_sku  = ss.sku
                                      AND ept_us.site        = 'US'
    WHERE  pm.holder_name      != 'UNASSIGNED'
      AND  (
               ss.dash_status = 'CRITICAL'
               OR (ss.dash_status = 'NO DATA' AND ss.stock <= %(oos_threshold)s)
           )
    ORDER  BY ss.sku
),

-- Step 6: Sibling SKUs — others under same parent_sku with enough stock
-- Compute base_sku (first segment before +) and parts count for type matching:
--   base_sku: SPLIT_PART(sku,'+',1) → PLTEBC, PLTERR, PLTEBS, PLWEFBC
--   parts=1 → simple (PLTEBC)
--   parts=2 → with accessory (PLTEBC+WCWYBM)
--   parts=3 → with accessory+bulb (PLTEBC+WCWYBM+ICST64E27)
-- Title priority: selected location → UK → US → global (ep.title)
siblings AS (
    SELECT DISTINCT ON (av.mapped_sku)
        av.mapped_sku                                                    AS sibling_sku,
        av.parent_sku,
        av.color                                                         AS sibling_color,
        av.asin                                                          AS sibling_asin,
        ls.total_stock                                                   AS sibling_stock,
        SPLIT_PART(av.mapped_sku, '+', 1)                                AS sibling_base,
        ARRAY_LENGTH(STRING_TO_ARRAY(av.mapped_sku, '+'), 1)             AS sibling_parts,
        COALESCE(
            NULLIF(TRIM(ept_loc.title), ''),
            NULLIF(TRIM(ept_uk.title),  ''),
            NULLIF(TRIM(ept_us.title),  ''),
            ep.title
        )                                                                AS sibling_name
    FROM   av_deduped           av
    JOIN   loc_stock            ls     ON ls.sku         = av.mapped_sku
    LEFT JOIN ebay_products     ep     ON ep.mapped_sku  = av.mapped_sku
    LEFT JOIN ebay_product_titles ept_loc
                                       ON ept_loc.mapped_sku = av.mapped_sku
                                      AND ept_loc.site       = %(location)s
    LEFT JOIN ebay_product_titles ept_uk
                                       ON ept_uk.mapped_sku  = av.mapped_sku
                                      AND ept_uk.site        = 'UK'
    LEFT JOIN ebay_product_titles ept_us
                                       ON ept_us.mapped_sku  = av.mapped_sku
                                      AND ept_us.site        = 'US'
    WHERE  ls.total_stock      >  %(min_alt_stock)s
    ORDER  BY av.mapped_sku, ls.total_stock DESC
),

-- Step 7: Three-level priority sibling selection
-- Priority 1: same base_sku + same parts (e.g. PLTEBC parts=1 → another PLTEBC parts=1)
-- Priority 2: different base_sku + same parts (e.g. PLTEBC parts=1 → PLTERR parts=1)
-- Priority 3: any sibling with stock (last resort fallback)
best_same_base_parts AS (
    SELECT DISTINCT ON (parent_sku, sibling_base, sibling_parts)
           parent_sku, sibling_base, sibling_parts,
           sibling_sku, sibling_color, sibling_asin, sibling_stock, sibling_name
    FROM   siblings
    ORDER  BY parent_sku, sibling_base, sibling_parts, sibling_stock DESC
),
best_same_parts AS (
    SELECT DISTINCT ON (parent_sku, sibling_parts)
           parent_sku, sibling_parts,
           sibling_sku, sibling_color, sibling_asin, sibling_stock, sibling_name
    FROM   siblings
    ORDER  BY parent_sku, sibling_parts, sibling_stock DESC
),
best_any AS (
    SELECT DISTINCT ON (parent_sku)
           parent_sku,
           sibling_sku, sibling_color, sibling_asin, sibling_stock, sibling_name
    FROM   siblings
    ORDER  BY parent_sku, sibling_stock DESC
)

-- Final: join OOS SKU with best sibling using 3-level priority
SELECT
    o.sku               AS out_of_stock_sku,
    o.stock             AS current_stock,
    o.dash_status       AS dashboard_status,
    o.avg_per_day,
    o.product_name,
    o.asin              AS current_asin,
    o.color             AS current_color,
    o.holder_name,
    o.parent_sku,
    COALESCE(
        CASE WHEN p1.sibling_sku != o.sku THEN p1.sibling_sku END,
        CASE WHEN p2.sibling_sku != o.sku THEN p2.sibling_sku END,
        CASE WHEN p3.sibling_sku != o.sku THEN p3.sibling_sku END
    )                   AS suggested_sku,
    COALESCE(
        CASE WHEN p1.sibling_sku != o.sku THEN p1.sibling_stock END,
        CASE WHEN p2.sibling_sku != o.sku THEN p2.sibling_stock END,
        CASE WHEN p3.sibling_sku != o.sku THEN p3.sibling_stock END
    )                   AS suggested_stock,
    COALESCE(
        CASE WHEN p1.sibling_sku != o.sku THEN p1.sibling_color END,
        CASE WHEN p2.sibling_sku != o.sku THEN p2.sibling_color END,
        CASE WHEN p3.sibling_sku != o.sku THEN p3.sibling_color END
    )                   AS suggested_color,
    COALESCE(
        CASE WHEN p1.sibling_sku != o.sku THEN p1.sibling_asin END,
        CASE WHEN p2.sibling_sku != o.sku THEN p2.sibling_asin END,
        CASE WHEN p3.sibling_sku != o.sku THEN p3.sibling_asin END
    )                   AS suggested_asin,
    COALESCE(
        CASE WHEN p1.sibling_sku != o.sku THEN p1.sibling_name END,
        CASE WHEN p2.sibling_sku != o.sku THEN p2.sibling_name END,
        CASE WHEN p3.sibling_sku != o.sku THEN p3.sibling_name END
    )                   AS suggested_name,
    CASE
        WHEN COALESCE(
            CASE WHEN p1.sibling_sku != o.sku THEN p1.sibling_sku END,
            CASE WHEN p2.sibling_sku != o.sku THEN p2.sibling_sku END,
            CASE WHEN p3.sibling_sku != o.sku THEN p3.sibling_sku END
        ) IS NULL THEN 'NO_ALTERNATIVE'
        ELSE 'REMAP_AVAILABLE'
    END                 AS remap_status
FROM oos o
-- P1: same base_sku + same parts count (best match — same product type, different color ok)
LEFT JOIN best_same_base_parts p1
       ON o.parent_sku IS NOT NULL
      AND p1.parent_sku   = o.parent_sku
      AND p1.sibling_base = SPLIT_PART(o.sku, '+', 1)
      AND p1.sibling_parts = ARRAY_LENGTH(STRING_TO_ARRAY(o.sku, '+'), 1)
-- P2: different base but same parts count (cross-color, same bundle type)
LEFT JOIN best_same_parts p2
       ON o.parent_sku IS NOT NULL
      AND p2.parent_sku    = o.parent_sku
      AND p2.sibling_parts = ARRAY_LENGTH(STRING_TO_ARRAY(o.sku, '+'), 1)
-- P3: any sibling (last resort)
LEFT JOIN best_any p3
       ON o.parent_sku IS NOT NULL
      AND p3.parent_sku = o.parent_sku
ORDER BY
    CASE o.dash_status WHEN 'CRITICAL' THEN 0 ELSE 1 END ASC,
    o.stock ASC,
    o.holder_name ASC
"""


# ── API function — called by dashboard_server.py ──────────────────────────────
def remap_suggestions():
    """
    Returns remap suggestions for a given location.
    Called via: GET /api/remap-suggestions?location=UK
    """
    location = request.args.get("location", "UK")

    # Validate location
    if location not in ("UK", "US", "Germany"):
        return jsonify({"error": "Invalid location. Use UK, US or Germany."}), 400

    try:
        conn = _pg_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(REMAP_QUERY, {
                "location":      location,
                "oos_threshold": OUT_OF_STOCK_THRESHOLD,
                "min_alt_stock": MIN_ALT_STOCK,
            })
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    results = []
    for r in rows:
        results.append({
            "out_of_stock_sku": r["out_of_stock_sku"],
            "current_stock":    int(r["current_stock"]    or 0),
            "dashboard_status": r["dashboard_status"]     or "CRITICAL",
            "product_name":     r["product_name"] or r["out_of_stock_sku"],
            "current_asin":     r["current_asin"]         or "",
            "current_color":    r["current_color"]        or "",
            "holder_name":      r["holder_name"]          or "",
            "parent_sku":       r["parent_sku"]           or "",
            "suggested_sku":    r["suggested_sku"]        or "",
            "suggested_stock":  int(r["suggested_stock"]  or 0) if r["suggested_stock"] else 0,
            "suggested_color":  r["suggested_color"]      or "",
            "suggested_asin":   r["suggested_asin"]       or "",
            "suggested_name":   r["suggested_name"] or "",
            "remap_status":     r["remap_status"],
        })

    # Get real synced_at from dashboard_cache for accurate stale detection
    # _saveRemapCache uses this value as REMAP_SYNC_KEY in sessionStorage
    # If this returns current time, stale detection never fires after DB sync
    real_sync_ts = datetime.utcnow().isoformat() + "Z"  # fallback
    try:
        conn2 = _pg_conn()
        with conn2.cursor() as cur2:
            cur2.execute(
                "SELECT MAX(synced_at) AT TIME ZONE 'UTC' FROM dashboard_cache "
                "WHERE location = %s", (location,)
            )
            ts_row = cur2.fetchone()
            if ts_row and ts_row[0]:
                ts = ts_row[0]
                real_sync_ts = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts,"strftime") else str(ts)
        conn2.close()
    except Exception:
        pass

    return jsonify({
        "location":   location,
        "total":      len(results),
        "results":    results,
        "thresholds": {
            "out_of_stock_at_or_below": OUT_OF_STOCK_THRESHOLD,
            "alternative_above":        MIN_ALT_STOCK,
        },
        "updated_at": real_sync_ts,
    })


# ── MySQL connection — for product detail enrichment ─────────────────────────
def _mysql_conn(database="listing_management"):
    """Direct MySQL connection for product detail queries (bullet points, images)."""
    import pymysql
    cfg = _load_config()
    my  = cfg.get("mysql", {})
    return pymysql.connect(
        host    = my.get("host",     "149.28.134.54"),
        port    = int(my.get("port", 3307)),
        user    = my.get("user",     "ledsone-db-system-user"),
        password= my.get("password", "r4315cgklqsj"),
        database= database,
        charset = "utf8mb4",
        cursorclass= pymysql.cursors.DictCursor,
        connect_timeout=8,
    )


# ── Product Detail API — reads from PostgreSQL (synced from MySQL) ────────────
def product_detail():
    """
    Fetches full product details from PostgreSQL ebay_products table.
    PostgreSQL is synced every 20 min from MySQL by db_sync.py.

    Title, ASIN (item_id), listing_url use site-specific priority:
      1. Selected location (UK / Germany / US)
      2. UK fallback
      3. US fallback
      4. Global fallback from ebay_products

    Confirmed from MySQL: 3,250 SKUs have different ASINs per marketplace.
    listing_url domain is site-specific (amazon.co.uk / .de / .com).
    Global ep.listing_url winner can be wrong domain (e.g. amazon.fr).
    """
    sku      = request.args.get("sku",      "").strip()
    location = request.args.get("location", "UK").strip()

    if not sku:
        return jsonify({"error": "sku parameter required"}), 400
    if location not in ("UK", "US", "Germany"):
        location = "UK"

    try:
        conn = _pg_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    ep.mapped_sku,
                    COALESCE(
                        NULLIF(TRIM(ept_loc.title), ''),
                        NULLIF(TRIM(ept_uk.title),  ''),
                        NULLIF(TRIM(ept_us.title),  ''),
                        ep.title
                    )                           AS title,
                    ep.which_channel,
                    COALESCE(
                        NULLIF(TRIM(ept_loc.item_id), ''),
                        NULLIF(TRIM(ept_uk.item_id),  ''),
                        NULLIF(TRIM(ept_us.item_id),  ''),
                        ep.item_id
                    )                           AS item_id,
                    ep.main_image_url,
                    ep.price,
                    ep.currency,
                    COALESCE(
                        NULLIF(TRIM(ept_loc.listing_url), ''),
                        NULLIF(TRIM(ept_uk.listing_url),  ''),
                        NULLIF(TRIM(ept_us.listing_url),  ''),
                        ep.listing_url
                    )                           AS listing_url,
                    ep.status,
                    ep.product_description,
                    ep.selected_variations
                FROM ebay_products ep
                LEFT JOIN ebay_product_titles ept_loc
                       ON ept_loc.mapped_sku = ep.mapped_sku
                      AND ept_loc.site       = %s
                LEFT JOIN ebay_product_titles ept_uk
                       ON ept_uk.mapped_sku  = ep.mapped_sku
                      AND ept_uk.site        = 'UK'
                LEFT JOIN ebay_product_titles ept_us
                       ON ept_us.mapped_sku  = ep.mapped_sku
                      AND ept_us.site        = 'US'
                WHERE ep.mapped_sku = %s
                LIMIT 1
            """, (location, sku))
            row = cur.fetchone()

            # Bullet points
            cur.execute("""
                SELECT point_text FROM product_bullet_points
                WHERE mapped_sku = %s ORDER BY view_order
            """, (sku,))
            bullets = [r["point_text"] for r in cur.fetchall()]

            # Sub images
            cur.execute("""
                SELECT DISTINCT image_url FROM product_sub_images
                WHERE mapped_sku = %s ORDER BY image_url
            """, (sku,))
            sub_imgs = [r["image_url"] for r in cur.fetchall()]

        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not row:
        return jsonify({"error": f"No product found for SKU: {sku}"}), 404

    # Strip HTML from product_description
    desc_raw = row.get("product_description") or ""
    desc_clean = re.sub(r"<[^>]+>", " ", desc_raw)
    desc_clean = re.sub(r"[ \t]+", " ", desc_clean)
    desc_clean = re.sub(r"\n{3,}", "\n\n", desc_clean).strip()
    desc_short = desc_clean[:1000] + ("…" if len(desc_clean) > 1000 else "")

    # Parse selected_variations JSON
    variations = []
    try:
        sv = row.get("selected_variations") or "[]"
        var_list = _json.loads(sv)
        if isinstance(var_list, list):
            for v in var_list:
                if isinstance(v, dict) and v.get("name") and v.get("value"):
                    variations.append({
                        "name":  str(v["name"]).strip(),
                        "value": str(v["value"]).strip(),
                    })
    except Exception:
        pass

    return jsonify({
        "sku":           row.get("mapped_sku") or sku,
        "title":         row.get("title") or "",
        "image_url":     row.get("main_image_url") or "",
        "listing_url":   row.get("listing_url") or "",
        "price":         float(row.get("price") or 0),
        "currency":      row.get("currency") or "GBP",
        "status":        row.get("status") or "",
        "channel":       row.get("which_channel") or "",
        "description":   desc_short,
        "variations":    variations,
        "asin":          row.get("item_id") or "",
        "bullet_points": bullets,
        "sub_images":    sub_imgs,
    })



def remap_summary():
    """
    Returns critical counts for all 3 locations from dashboard_cache.
    Replaces 3 separate /api/remap-suggestions calls in loadLocCounts().
    dashboard_cache has pre-computed status per SKU per location.
    One lightweight GROUP BY query instead of 3 expensive 7-CTE queries.
    """
    try:
        conn = _pg_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    location,
                    COUNT(*) FILTER (WHERE status='CRITICAL')                  AS critical,
                    COUNT(*) FILTER (WHERE status='NO DATA' AND stock <= 5)    AS nodata_oos,
                    COUNT(*)                                                      AS total,
                    MAX(synced_at) AT TIME ZONE 'UTC'                          AS synced_at
                FROM dashboard_cache
                WHERE location IN ('UK','US','Germany')
                GROUP BY location
            """)
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    result = {}
    sync_ts = ""
    for row in rows:
        result[row["location"]] = {
            "critical":   int(row["critical"]   or 0),
            "nodata_oos": int(row["nodata_oos"] or 0),
            "total":      int(row["total"]      or 0),
        }
        # synced_at is same for all locations (written in same db_sync run)
        if row["synced_at"] and not sync_ts:
            sync_ts = row["synced_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(row["synced_at"], "strftime") else str(row["synced_at"])

    result["updated_at"] = sync_ts or datetime.utcnow().isoformat() + "Z"
    return jsonify(result)


def get_remap_html():
    return REMAP_HTML


REMAP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Remap Engine &mdash; LEDs One</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ════════════════════════════════════════════════
   REMAP ENGINE — Completely unique design
   Font: Inter + JetBrains Mono
   Layout: Left sidebar + main content area
   Theme: Deep slate + accent colours
   ════════════════════════════════════════════════ */
:root{
  --bg:#080c14;
  --panel:#0e1420;
  --card:#131c2c;
  --card2:#18233a;
  --line:#1e293b;
  --line2:#243046;
  --text:#cbd5e1;
  --text2:#94a3b8;
  --text3:#64748b;
  --white:#f1f5f9;
  --red:#f87171;
  --red-d:#dc2626;
  --red-glow:rgba(248,113,113,.15);
  --amber:#fbbf24;
  --amber-d:#d97706;
  --amber-glow:rgba(251,191,36,.1);
  --emerald:#34d399;
  --emerald-d:#059669;
  --emerald-glow:rgba(52,211,153,.1);
  --sky:#38bdf8;
  --sky-glow:rgba(56,189,248,.1);
  --violet:#a78bfa;
  --sidebar-w:240px;
}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;overflow:hidden;}
body{background:var(--bg);color:var(--text);font-family:"Inter",sans-serif;font-size:13px;display:flex;}

/* ══ SIDEBAR ══════════════════════════════════════ */
.sidebar{width:var(--sidebar-w);min-width:var(--sidebar-w);background:var(--panel);
  border-right:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden;}

.sb-brand{padding:18px 16px 14px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:10px;}
.sb-brand .logo{width:28px;height:28px;background:linear-gradient(135deg,#dc2626,#f97316);
  border-radius:7px;display:flex;align-items:center;justify-content:center;font-size:0.85rem;flex-shrink:0;}
.sb-brand .brand-text{flex:1;min-width:0;}
.sb-brand .brand-name{font-size:0.78rem;font-weight:700;color:var(--white);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.sb-brand .brand-sub{font-size:0.6rem;color:var(--text3);margin-top:1px;}

.sb-section{padding:12px 10px 6px;font-size:0.58rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--text3);}

.sb-mkt-btn{display:flex;align-items:center;gap:9px;padding:8px 12px;border-radius:8px;cursor:pointer;
  transition:all .15s;margin:0 6px 2px;border:none;background:none;color:var(--text2);
  font-family:"Inter",sans-serif;font-size:0.78rem;font-weight:500;width:calc(100% - 12px);}
.sb-mkt-btn:hover{background:var(--card);color:var(--white);}
.sb-mkt-btn.active{background:var(--card2);color:var(--white);font-weight:600;}
.sb-mkt-btn .mkt-flag{font-size:1rem;flex-shrink:0;}
.sb-mkt-btn .mkt-name{flex:1;text-align:left;}
.sb-mkt-btn .mkt-cnt{background:rgba(255,255,255,.06);border-radius:999px;padding:1px 7px;
  font-size:0.6rem;font-family:"JetBrains Mono",monospace;color:var(--text3);}
.sb-mkt-btn.active .mkt-cnt{background:rgba(56,189,248,.15);color:var(--sky);}
.sb-mkt-btn .mkt-crit{background:rgba(248,113,113,.15);border-radius:999px;padding:1px 6px;
  font-size:0.6rem;font-family:"JetBrains Mono",monospace;color:var(--red);}

.sb-divider{height:1px;background:var(--line);margin:8px 0;}

.sb-holder-scroll{flex:1;overflow-y:auto;padding-bottom:8px;}
.sb-holder-scroll::-webkit-scrollbar{width:3px;}
.sb-holder-scroll::-webkit-scrollbar-thumb{background:var(--line2);border-radius:2px;}
.hbtn-sb{display:flex;align-items:center;gap:8px;padding:6px 12px;border-radius:6px;cursor:pointer;
  transition:all .12s;margin:0 6px 1px;border:none;background:none;color:var(--text3);
  font-family:"Inter",sans-serif;font-size:0.75rem;font-weight:500;width:calc(100% - 12px);}
.hbtn-sb:hover{background:var(--card);color:var(--text);}
.hbtn-sb.active{background:rgba(56,189,248,.08);color:var(--sky);font-weight:600;}
.hbtn-sb .hb-dot{width:6px;height:6px;border-radius:50%;background:var(--line2);flex-shrink:0;}
.hbtn-sb.active .hb-dot{background:var(--sky);}
.hbtn-sb .hb-name{flex:1;text-align:left;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.hbtn-sb .hb-cnt{font-size:0.62rem;font-family:"JetBrains Mono",monospace;color:var(--text3);}
.hbtn-sb.active .hb-cnt{color:var(--sky);}
.hbtn-sb .hb-crit{font-size:0.6rem;font-family:"JetBrains Mono",monospace;color:var(--red);
  background:rgba(248,113,113,.12);border-radius:999px;padding:0px 5px;}

.sb-footer{padding:10px 12px;border-top:1px solid var(--line);}
.sb-back{display:flex;align-items:center;gap:7px;padding:7px 10px;border-radius:6px;
  background:rgba(56,189,248,.07);border:1px solid rgba(56,189,248,.15);
  color:var(--sky);text-decoration:none;font-size:0.73rem;font-weight:600;
  transition:all .15s;}
.sb-back:hover{background:rgba(56,189,248,.12);}

/* ══ MAIN AREA ════════════════════════════════════ */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;}

/* Top bar */
.topbar{height:48px;background:var(--panel);border-bottom:1px solid var(--line);
  display:flex;align-items:center;padding:0 20px;gap:12px;flex-shrink:0;}
.tb-title{font-size:0.85rem;font-weight:700;color:var(--white);}
.tb-loc{font-size:0.7rem;color:var(--text3);font-family:"JetBrains Mono",monospace;
  background:var(--card);border:1px solid var(--line2);border-radius:4px;padding:2px 8px;}
.tb-spacer{flex:1;}
.tb-search{background:var(--card);border:1px solid var(--line2);color:var(--text);
  border-radius:6px;padding:6px 12px;font-size:0.78rem;font-family:"Inter",sans-serif;
  width:220px;}
.tb-search:focus{outline:none;border-color:var(--sky);}
.tb-select{background:var(--card);border:1px solid var(--line2);color:var(--text);
  border-radius:6px;padding:6px 10px;font-size:0.78rem;font-family:"Inter",sans-serif;cursor:pointer;}
.tb-ts{font-size:0.65rem;color:var(--text3);font-family:"JetBrains Mono",monospace;white-space:nowrap;}

/* Metrics strip */
.metrics{display:flex;gap:1px;background:var(--line);border-bottom:1px solid var(--line);flex-shrink:0;}
.metric{flex:1;background:var(--panel);padding:14px 18px;cursor:pointer;transition:all .15s;position:relative;overflow:hidden;}
.metric:hover{background:var(--card);}
.metric.active{background:var(--card2);}
.metric::after{content:"";position:absolute;bottom:0;left:0;right:0;height:3px;background:transparent;transition:background .15s;}
.metric.m-crit.active::after{background:var(--red);}
.metric.m-avail.active::after{background:var(--emerald);}
.metric.m-noalt.active::after{background:var(--amber);}
.metric.m-total.active::after{background:var(--sky);}
.metric.m-nodata.active::after{background:#64748b;}
/* Left accent bar on active */
.metric::before{content:"";position:absolute;top:0;left:0;bottom:0;width:3px;background:transparent;transition:background .15s;}
.metric.m-crit.active::before{background:var(--red);}
.metric.m-avail.active::before{background:var(--emerald);}
.metric.m-noalt.active::before{background:var(--amber);}
.metric.m-total.active::before{background:var(--sky);}
.metric.m-nodata.active::before{background:#64748b;}
.m-label{font-size:0.57rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--text3);margin-bottom:7px;display:flex;align-items:center;gap:5px;}
.m-val{font-size:1.75rem;font-weight:800;font-family:"JetBrains Mono",monospace;line-height:1;letter-spacing:-0.02em;}
.metric.m-crit  .m-val{color:var(--red);}
.metric.m-avail .m-val{color:var(--emerald);}
.metric.m-noalt .m-val{color:var(--amber);}
.metric.m-total .m-val{color:var(--sky);}
.metric.m-nodata .m-val{color:#94a3b8;}
.metric.m-nodata:hover{background:rgba(71,85,105,.1);}
.m-sub{font-size:0.6rem;color:var(--text3);margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}

/* Content scroll area — both axes scroll at the content level
   so the horizontal scrollbar is always at the bottom of the visible window */
.content{flex:1;overflow-y:auto;overflow-x:auto;padding:16px 20px;}
.content::-webkit-scrollbar{width:4px;height:6px;}
.content::-webkit-scrollbar-thumb{background:var(--line2);border-radius:2px;}
.content::-webkit-scrollbar-track{background:transparent;}
.content::-webkit-scrollbar-corner{background:transparent;}

/* Section header */
.sec-hdr{display:flex;align-items:center;gap:10px;margin-bottom:10px;padding:6px 0;}
.sec-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;font-size:0.62rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;}
.sec-badge.sb-crit{background:var(--red-glow);border:1px solid rgba(248,113,113,.3);color:var(--red);}
.sec-badge.sb-avail{background:var(--emerald-glow);border:1px solid rgba(52,211,153,.3);color:var(--emerald);}
.sec-badge.sb-noalt{background:var(--amber-glow);border:1px solid rgba(251,191,36,.3);color:var(--amber);}
.sec-badge .sb-pulse{width:6px;height:6px;border-radius:50%;}
.sec-badge.sb-crit .sb-pulse{background:var(--red);box-shadow:0 0 6px var(--red);}
.sec-badge.sb-avail .sb-pulse{background:var(--emerald);}
.sec-badge.sb-noalt .sb-pulse{background:var(--amber);}
.sec-badge.sb-nodata{background:rgba(71,85,105,.12);border:1px solid rgba(100,116,139,.3);color:#94a3b8;}
.sec-badge.sb-nodata .sb-pulse{background:#64748b;}
.sec-count{font-size:0.65rem;color:var(--text3);font-family:"JetBrains Mono",monospace;}
.sec-line{flex:1;height:1px;background:var(--line);}
.sec-mb{margin-bottom:20px;}

/* Product row card — horizontal card layout (not a table) */
.prod-list{display:flex;flex-direction:column;gap:6px;margin-bottom:4px;}
.prod-card{background:var(--card);border:1px solid var(--line2);border-radius:10px;
  padding:10px 14px;display:grid;
  grid-template-columns:200px 1fr 58px 180px 120px 70px 160px 128px 68px;
  gap:8px;align-items:center;transition:all .15s;cursor:default;}
.prod-card:hover{background:var(--card2);border-color:#2d3b54;}
.prod-card.pc-crit{border-left:3px solid var(--red-d);background:rgba(220,38,38,.03);}
.prod-card.pc-crit:hover{background:rgba(220,38,38,.07);}
.prod-card.pc-nodata{border-left:3px solid #475569;background:rgba(71,85,105,.04);}
.prod-card.pc-nodata:hover{background:rgba(71,85,105,.09);}
.prod-card.pc-low{border-left:3px solid var(--amber-d);}
.prod-col{min-width:0;position:relative;}
/* SKU */
.pc-sku{font-family:"JetBrains Mono",monospace;font-size:0.68rem;color:var(--white);
  word-break:break-all;white-space:normal;line-height:1.4;font-weight:500;}
/* Product name — ellipsis with full name on cursor hover via title attr */
.pc-name{font-size:0.75rem;color:var(--text);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;cursor:default;position:relative;}
.pc-name:hover{color:var(--white);}

/* Tooltip — show full product name on hover */
.pc-name[title]:hover::after{
  content:attr(title);
  position:absolute;
  left:0;top:calc(100% + 6px);
  background:#1e2d45;border:1px solid var(--line2);
  color:var(--white);font-size:0.72rem;line-height:1.5;
  padding:7px 12px;border-radius:7px;
  white-space:normal;word-break:break-word;
  max-width:420px;min-width:200px;
  z-index:999;
  box-shadow:0 4px 16px rgba(0,0,0,.5);
  pointer-events:none;
}
/* Stock bubble */
.pc-stock{text-align:center;}
.st-zero{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;
  border-radius:50%;background:rgba(220,38,38,.15);border:2px solid rgba(220,38,38,.4);
  color:var(--red);font-size:0.78rem;font-weight:800;font-family:"JetBrains Mono",monospace;}
.st-low{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;
  border-radius:50%;background:rgba(217,119,6,.15);border:2px solid rgba(217,119,6,.4);
  color:var(--amber);font-size:0.78rem;font-weight:800;font-family:"JetBrains Mono",monospace;}
.st-crit-low{display:inline-flex;align-items:center;justify-content:center;
  border-radius:5px;background:rgba(220,38,38,.12);border:1px solid rgba(220,38,38,.35);
  color:var(--red);font-size:0.72rem;font-weight:800;font-family:"JetBrains Mono",monospace;
  padding:2px 7px;}
/* Suggested SKU */
.pc-ssku{font-family:"JetBrains Mono",monospace;font-size:0.65rem;color:var(--text2);
  word-break:break-all;white-space:normal;line-height:1.4;}
/* Available stock */
.pc-avail{text-align:center;}
.av-pill{display:inline-block;background:rgba(5,150,105,.15);border:1px solid rgba(52,211,153,.25);
  color:var(--emerald);border-radius:5px;padding:2px 8px;font-size:0.7rem;font-weight:700;
  font-family:"JetBrains Mono",monospace;}
/* Status tag */
.pc-status{}
.stag{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;border-radius:5px;
  font-size:0.6rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase;white-space:nowrap;}
.stag::before{content:"";width:5px;height:5px;border-radius:50%;flex-shrink:0;}
.stag.REMAP_AVAILABLE{background:rgba(5,150,105,.12);color:var(--emerald);border:1px solid rgba(52,211,153,.2);}
.stag.REMAP_AVAILABLE::before{background:var(--emerald);}
.stag.NO_ALTERNATIVE{background:rgba(217,119,6,.1);color:var(--amber);border:1px solid rgba(251,191,36,.2);}
.stag.NO_ALTERNATIVE::before{background:var(--amber);}
/* Holder tag */
.pc-holder{}
.holder-tag{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:6px;
  background:rgba(255,255,255,.04);border:1px solid var(--line2);
  font-size:0.68rem;font-weight:600;color:var(--text);cursor:pointer;transition:all .12s;
  white-space:nowrap;width:105px;min-width:105px;max-width:105px;overflow:hidden;
  text-overflow:ellipsis;justify-content:center;}
.holder-tag:hover{background:rgba(56,189,248,.08);border-color:rgba(56,189,248,.3);color:var(--sky);}
/* Action */
.pc-action{display:flex;justify-content:center;}
.det-btn{background:rgba(56,189,248,.08);border:1px solid rgba(56,189,248,.2);
  color:var(--sky);border-radius:6px;padding:5px 12px;font-size:0.7rem;font-weight:600;
  cursor:pointer;font-family:"Inter",sans-serif;white-space:nowrap;transition:all .15s;}
.det-btn:hover{background:rgba(56,189,248,.15);border-color:var(--sky);}
/* Column headers */
.col-hdr{display:grid;
  grid-template-columns:200px 1fr 58px 180px 120px 70px 160px 128px 68px;
  gap:8px;padding:4px 14px 6px;align-items:center;}
.ch{font-size:0.58rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--text3);position:relative;}
.ch.center{text-align:center;}
/* Empty / loading */
.empty-msg{background:var(--card);border:1px solid var(--line2);border-radius:10px;
  padding:32px;text-align:center;color:var(--text3);font-size:0.78rem;}
@keyframes shimmer{0%{background-position:-800px 0}100%{background-position:800px 0}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes fadeInUp{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.loading-msg{padding:32px;text-align:center;color:var(--text3);font-size:0.78rem;}
.loader-wrap{display:flex;flex-direction:column;align-items:center;gap:12px;padding:48px 24px;}
.loader-ring{width:32px;height:32px;border:3px solid rgba(255,255,255,.07);
  border-top-color:var(--sky);border-radius:50%;animation:spin .7s linear infinite;}
.loader-label{font-size:0.72rem;color:var(--text3);font-family:"JetBrains Mono",monospace;}
.skeleton-card{height:44px;border-radius:8px;margin-bottom:5px;
  background:linear-gradient(90deg,var(--card) 25%,rgba(255,255,255,.04) 50%,var(--card) 75%);
  background-size:800px 100%;animation:shimmer 1.5s infinite linear;}
.fade-in-up{animation:fadeInUp .25s ease both;}
/* rc */
.rc{font-size:0.65rem;color:var(--text3);font-family:"JetBrains Mono",monospace;
  padding:4px 0 10px;}

/* ══ MODAL ════════════════════════════════════════ */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);
  z-index:500;align-items:flex-start;justify-content:center;padding:24px;overflow-y:auto;}
.modal-overlay.open{display:flex;}
.modal{background:var(--panel);border:1px solid var(--line2);border-radius:14px;
  width:100%;max-width:880px;margin:auto;box-shadow:0 24px 64px rgba(0,0,0,.7);}
/* Modal header */
.mhdr{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;align-items:flex-start;justify-content:space-between;}
.mhdr-left h2{font-size:0.95rem;font-weight:700;color:var(--white);}
.mhdr-left .msub{font-size:0.68rem;color:var(--text3);margin-top:3px;}
.mhdr-close{background:var(--card);border:1px solid var(--line2);color:var(--text2);
  border-radius:6px;width:26px;height:26px;cursor:pointer;font-size:0.82rem;
  display:flex;align-items:center;justify-content:center;transition:all .15s;flex-shrink:0;}
.mhdr-close:hover{background:var(--card2);color:var(--white);}
/* Strip */
.mstrip{display:flex;flex-wrap:wrap;gap:18px;padding:10px 22px;border-bottom:1px solid var(--line);background:var(--bg);}
.ms-it{font-size:0.7rem;color:var(--text3);}
.ms-it strong{color:var(--text);}
/* Body */
.mbody{padding:22px;}
/* Compare */
.cmp{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px;}
.cmpcard{background:var(--bg);border:1px solid var(--line2);border-radius:10px;overflow:hidden;}
.cmpcard.cur{border-color:rgba(220,38,38,.3);}
.cmpcard.sug{border-color:rgba(5,150,105,.3);}
.cmpcard-hdr{padding:9px 14px;border-bottom:1px solid var(--line);font-size:0.6rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;}
.cmpcard.cur .cmpcard-hdr{background:rgba(220,38,38,.07);color:#fca5a5;}
.cmpcard.sug .cmpcard-hdr{background:rgba(5,150,105,.07);color:#6ee7b7;}
.cmpcard-body{padding:14px;display:flex;flex-direction:column;gap:10px;}
.cfield{}.cfl{font-size:0.57rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text3);margin-bottom:2px;}
.cfv{font-size:0.77rem;color:var(--text);word-break:break-all;}
.cfv.mono{font-family:"JetBrains Mono",monospace;font-size:0.7rem;}
.cfv.na{color:var(--text3);font-style:italic;}
.alink{color:var(--sky);text-decoration:none;font-family:"JetBrains Mono",monospace;font-size:0.7rem;}
.alink:hover{text-decoration:underline;}
/* Map info */
.minfo{background:var(--bg);border:1px solid var(--line2);border-radius:10px;margin-bottom:16px;overflow:hidden;}
.minfo-hdr{padding:9px 14px;border-bottom:1px solid var(--line);font-size:0.6rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--sky);background:rgba(56,189,248,.06);}
.mgrid{display:grid;grid-template-columns:repeat(3,1fr);}
.mfield{padding:10px 14px;border-right:1px solid var(--line);border-bottom:1px solid var(--line);}
.mfield:nth-child(3n){border-right:none;}
.mfield:nth-last-child(-n+3){border-bottom:none;}
.mfl{font-size:0.57rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text3);margin-bottom:3px;}
.mfv{font-size:0.77rem;color:var(--text);}
/* Checklist */
.clist{background:var(--bg);border:1px solid var(--line2);border-radius:10px;overflow:hidden;}
.clist-hdr{padding:9px 14px;border-bottom:1px solid var(--line);font-size:0.6rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--amber);background:rgba(217,119,6,.06);}
.clist-body{padding:12px 14px;display:flex;flex-direction:column;gap:6px;}
.ci{display:flex;align-items:flex-start;gap:9px;padding:5px 6px;border-radius:6px;cursor:pointer;transition:background .1s;}
.ci:hover{background:rgba(255,255,255,.03);}
.ci input{margin-top:1px;accent-color:var(--sky);flex-shrink:0;}
.ci span{font-size:0.74rem;color:var(--text2);line-height:1.4;}
/* Modal actions */
.mact{padding:14px 22px;border-top:1px solid var(--line);display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap;background:var(--bg);border-radius:0 0 14px 14px;}
.btn-az{background:#ff9900;border:none;color:#000;border-radius:7px;padding:7px 14px;font-size:0.73rem;font-weight:700;cursor:pointer;font-family:"Inter",sans-serif;text-decoration:none;display:inline-flex;align-items:center;gap:5px;transition:opacity .15s;}
.btn-az:hover{opacity:.88;}
.btn-sg{background:rgba(5,150,105,.12);border:1px solid rgba(52,211,153,.25);color:var(--emerald);border-radius:7px;padding:7px 14px;font-size:0.73rem;font-weight:700;cursor:pointer;font-family:"Inter",sans-serif;text-decoration:none;display:inline-flex;align-items:center;gap:5px;transition:all .15s;}
.btn-sg:hover{background:rgba(5,150,105,.2);}
.btn-cl{background:var(--card);border:1px solid var(--line2);color:var(--text2);border-radius:7px;padding:7px 14px;font-size:0.73rem;cursor:pointer;font-family:"Inter",sans-serif;transition:all .15s;}
.btn-cl:hover{background:var(--card2);color:var(--white);}
/* ── Table scroll wrapper — width enforcer only, parent .content scrolls ── */
.tbl-scroll{overflow:visible;}
.tbl-inner{min-width:1060px;}

@media(max-width:1400px){
  .prod-card,.col-hdr{grid-template-columns:180px 1fr 58px 160px 100px 70px 150px 118px 68px;}
}
@media(max-width:1100px){
  .prod-card,.col-hdr{grid-template-columns:140px 1fr 70px 140px 70px 100px 100px 70px;}
  .tbl-inner{min-width:960px;}
}
@media(max-width:900px){
  .sidebar{display:none;}
  .cmp{grid-template-columns:1fr;}
  .mgrid{grid-template-columns:1fr 1fr;}
  .mfield:nth-child(3n){border-right:1px solid var(--line);}
  .mfield:nth-child(2n){border-right:none;}
  .prod-card,.col-hdr{grid-template-columns:140px 1fr 70px 140px 70px 100px 100px 70px;}
  .tbl-inner{min-width:880px;}
}
.det-loading{padding:14px;font-size:0.75rem;color:var(--text3);text-align:center;}
/* ── Product detail enrichment sections ── */
.det-loading{text-align:center;padding:16px;color:var(--text3);font-size:0.75rem;}
.img-gallery{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;}
.img-main{width:100%;max-height:220px;object-fit:contain;border-radius:8px;
  background:var(--bg);border:1px solid var(--line2);display:block;}
.img-thumb-wrap{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px;}
.img-thumb{width:64px;height:64px;object-fit:cover;border-radius:6px;cursor:pointer;
  border:1px solid var(--line2);background:var(--bg);transition:border-color .15s;}
.img-thumb:hover,.img-thumb.active{border-color:var(--sky);}
.img-section{background:var(--bg);border:1px solid var(--line2);border-radius:10px;margin-bottom:14px;overflow:hidden;}
.img-section-hdr{padding:8px 14px;border-bottom:1px solid var(--line);font-size:0.6rem;font-weight:700;
  letter-spacing:.08em;text-transform:uppercase;color:var(--violet);background:rgba(167,139,250,.06);}
.img-section-body{padding:12px 14px;}
.bullet-section{background:var(--bg);border:1px solid var(--line2);border-radius:10px;margin-bottom:14px;overflow:hidden;}
.bullet-section-hdr{padding:8px 14px;border-bottom:1px solid var(--line);font-size:0.6rem;font-weight:700;
  letter-spacing:.08em;text-transform:uppercase;color:var(--emerald);background:rgba(52,211,153,.06);}
.bullet-list{padding:10px 14px;list-style:none;display:flex;flex-direction:column;gap:6px;}
.bullet-list li{display:flex;align-items:flex-start;gap:8px;font-size:0.75rem;color:var(--text);line-height:1.5;}
.bullet-list li::before{content:"•";color:var(--emerald);font-size:0.9rem;flex-shrink:0;margin-top:1px;}
.price-tag{display:inline-flex;align-items:center;gap:4px;background:rgba(251,191,36,.1);
  border:1px solid rgba(251,191,36,.2);border-radius:5px;padding:2px 9px;
  font-size:0.72rem;font-weight:700;font-family:"JetBrains Mono",monospace;color:var(--amber);}
.detail-meta-row{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px;align-items:center;}
</style>
</head>
<body>

<!-- ══ SIDEBAR ════════════════════════════════════ -->
<div class="sidebar">
  <div class="sb-brand">
    <div class="logo">&#9889;</div>
    <div class="brand-text">
      <div class="brand-name">Remap Engine</div>
      <div class="brand-sub">LEDs One Automation</div>
    </div>
  </div>

  <div class="sb-section">Marketplace</div>
  <button class="sb-mkt-btn active" data-loc="UK" onclick="selectLoc('UK',this)">
    <span class="mkt-flag">&#127468;&#127463;</span>
    <span class="mkt-name">United Kingdom</span>
    <span class="mkt-cnt" id="mc-UK">—</span>
    <span class="mkt-crit" id="mc-UK-c" style="display:none;"></span>
  </button>
  <button class="sb-mkt-btn" data-loc="US" onclick="selectLoc('US',this)">
    <span class="mkt-flag">&#127482;&#127480;</span>
    <span class="mkt-name">United States</span>
    <span class="mkt-cnt" id="mc-US">—</span>
    <span class="mkt-crit" id="mc-US-c" style="display:none;"></span>
  </button>
  <button class="sb-mkt-btn" data-loc="Germany" onclick="selectLoc('Germany',this)">
    <span class="mkt-flag">&#127465;&#127466;</span>
    <span class="mkt-name">Germany</span>
    <span class="mkt-cnt" id="mc-Germany">—</span>
    <span class="mkt-crit" id="mc-Germany-c" style="display:none;"></span>
  </button>

  <div class="sb-divider"></div>
  <div class="sb-section">Portfolio Holders</div>
  <div class="sb-holder-scroll" id="sb-holders">
    <div style="padding:8px 16px;font-size:0.7rem;color:var(--text3);">Loading&hellip;</div>
  </div>

  <div class="sb-footer">
    <a href="/" class="sb-back">&#8592; Stock Dashboard</a>
  </div>
</div>

<!-- ══ MAIN AREA ══════════════════════════════════ -->
<div class="main">

  <!-- Top bar -->
  <div class="topbar">
    <span class="tb-title">Remap Suggestions</span>
    <span class="tb-loc" id="tb-loc">UK</span>
    <div class="tb-spacer"></div>
    <input type="search" class="tb-search" id="qbox" placeholder="&#128269;  Search SKU, name, holder..." oninput="applyFilters()">
    <select class="tb-select" id="sf" onchange="onStatusDrop()">
      <option value="">All Statuses</option>
      <option value="REMAP_AVAILABLE">Remap Available</option>
      <option value="NO_ALTERNATIVE">No Alternative</option>
    </select>
    <span class="tb-ts" id="ts">—</span>
  </div>

  <!-- Metrics strip -->
  <div class="metrics">
    <div class="metric m-crit" id="mc-crit" onclick="setFilter('CRITICAL_ZERO')">
      <div class="m-label">&#9888; Critical (Days &le;7)</div>
      <div class="m-val" id="s-crit">—</div>
      <div class="m-sub">Dashboard CRITICAL status</div>
    </div>
    <div class="metric m-nodata" id="mc-nodata" onclick="setFilter('NO_DATA_ZERO')">
      <div class="m-label">&#128203; No Data + Zero Stock</div>
      <div class="m-val" id="s-nodata">—</div>
      <div class="m-sub">No sales history, empty</div>
    </div>
    <div class="metric m-avail" id="mc-avail" onclick="setFilter('REMAP_AVAILABLE')">
      <div class="m-label">Remap Available</div>
      <div class="m-val" id="s-avail">—</div>
      <div class="m-sub">Alternative found</div>
    </div>
    <div class="metric m-noalt" id="mc-noalt" onclick="setFilter('NO_ALTERNATIVE')">
      <div class="m-label">No Alternative</div>
      <div class="m-val" id="s-noalt">—</div>
      <div class="m-sub">Manual review needed</div>
    </div>
    <div class="metric m-total" id="mc-total" onclick="setFilter('')">
      <div class="m-label">Total Checked</div>
      <div class="m-val" id="s-total">—</div>
      <div class="m-sub" id="s-loc">UK marketplace</div>
    </div>
  </div>

  <!-- Scrollable content -->
  <div class="content" id="content">
    <div class="loading-msg">Loading data&hellip;</div>
  </div>

</div><!-- /main -->

<!-- ══ DETAIL MODAL ════════════════════════════════ -->
<div class="modal-overlay" id="modal-overlay" onclick="closeModal(event)">
  <div class="modal">
    <div class="mhdr">
      <div class="mhdr-left">
        <h2 id="m-title">Detail</h2>
        <div class="msub" id="m-sub">Product comparison</div>
      </div>
      <button class="mhdr-close" onclick="closeModalDirect()">&#10005;</button>
    </div>
    <div class="mstrip" id="m-strip"></div>
    <div class="mbody">
      <div class="cmp" id="m-cmp"></div>

      <!-- Enriched product detail — Current SKU -->
      <div class="minfo" style="margin-bottom:14px;">
        <div class="minfo-hdr">&#128270; Current Product Detail</div>
        <div style="padding:12px 14px;" id="m-detail-cur"></div>
      </div>

      <!-- Enriched product detail — Suggested SKU -->
      <div class="minfo" style="margin-bottom:14px;" id="m-detail-sug-wrap">
        <div class="minfo-hdr" style="color:var(--emerald);background:rgba(52,211,153,.06);">&#10003; Suggested Product Detail</div>
        <div style="padding:12px 14px;" id="m-detail-sug"></div>
      </div>

      <div class="minfo">
        <div class="minfo-hdr">&#9432; Mapping &amp; Threshold Information</div>
        <div class="mgrid" id="m-map"></div>
      </div>
      <div class="clist">
        <div class="clist-hdr">&#9997; Action Checklist</div>
        <div class="clist-body">
          <label class="ci"><input type="checkbox"><span>Verify same product design and specifications</span></label>
          <label class="ci"><input type="checkbox"><span>Confirm only color or variant differs — not the product category</span></label>
          <label class="ci"><input type="checkbox"><span>Update Amazon listing SKU mapping to replacement SKU</span></label>
          <label class="ci"><input type="checkbox"><span>Replace product images to show the new color variant</span></label>
          <label class="ci"><input type="checkbox"><span>Update listing title if original color name is mentioned</span></label>
          <label class="ci"><input type="checkbox"><span>Update bullet points if color or variant is referenced</span></label>
          <label class="ci"><input type="checkbox"><span>Verify listing is active 24&ndash;48 hours after update</span></label>
          <label class="ci"><input type="checkbox"><span>Confirm stock reflects correctly on Amazon Seller Central</span></label>
        </div>
      </div>
    </div>
    <div class="mact">
      <a id="lnk-cur" href="#" target="_blank" class="btn-az">&#128279; Current Listing</a>
      <a id="lnk-sug" href="#" target="_blank" class="btn-sg">&#10003; Suggested Listing</a>
      <button class="btn-cl" onclick="closeModalDirect()">Close</button>
    </div>
  </div>
</div>

<script>
var allRows=[], activeLoc='UK', activeHolder='ALL', activeFilter='';
var _filtered=[];

// ── sessionStorage state — persists across navigations ────────────────────────
// Fixes Issue 3: returning to Remap from Dashboard no longer re-fetches
// Fixes Issue 4: Back button from Product Details restores Remap state
var REMAP_CACHE_KEY = 'ledsone_remap_cache';
var REMAP_STATE_KEY = 'ledsone_remap_state';
var REMAP_SYNC_KEY  = 'ledsone_remap_sync_ts'; // server's last sync timestamp
var REMAP_CACHE_TTL = 25 * 60 * 1000; // 25 min hard expiry (sync every 20 min)

function _saveRemapCache(data, loc, serverSyncTs){
  try{
    sessionStorage.setItem(REMAP_CACHE_KEY, JSON.stringify({rows:data, loc:loc, ts:Date.now()}));
    if(serverSyncTs) sessionStorage.setItem(REMAP_SYNC_KEY, serverSyncTs);
  }catch(e){}
}
function _getCachedRemapSyncTs(){
  try{ return sessionStorage.getItem(REMAP_SYNC_KEY) || ''; }catch(e){ return ''; }
}
function _isRemapCacheStale(serverUpdatedAt){
  var cachedTs = _getCachedRemapSyncTs();
  if(!cachedTs) return true;
  return serverUpdatedAt > cachedTs; // ISO string comparison — lexicographic = chronological
}
function _saveRemapState(){
  try{
    sessionStorage.setItem(REMAP_STATE_KEY, JSON.stringify({
      activeLoc:activeLoc, activeHolder:activeHolder, activeFilter:activeFilter,
      qbox: document.getElementById('qbox') ? document.getElementById('qbox').value : '',
      sf: document.getElementById('sf') ? document.getElementById('sf').value : '',
      scroll: document.getElementById('content') ? document.getElementById('content').scrollTop : 0
    }));
  }catch(e){}
}
function _loadRemapCache(){
  try{
    var raw = sessionStorage.getItem(REMAP_CACHE_KEY);
    if(!raw) return null;
    var d = JSON.parse(raw);
    if(Date.now() - d.ts > REMAP_CACHE_TTL) return null; // hard 25-min expiry
    return d;
  }catch(e){ return null; }
}
function _loadRemapState(){
  try{
    var raw = sessionStorage.getItem(REMAP_STATE_KEY);
    return raw ? JSON.parse(raw) : null;
  }catch(e){ return null; }
}

function E(i){return document.getElementById(i);}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

// ── Marketplace selection ─────────────────────────────────────────────────────
function selectLoc(loc, el){
  activeLoc=loc; activeHolder='ALL'; activeFilter=''; E('sf').value='';
  document.querySelectorAll('.sb-mkt-btn').forEach(function(b){b.classList.toggle('active',b===el);});
  document.querySelectorAll('.metric').forEach(function(m){m.classList.remove('active');});

  // Remap is location-specific — ALL MARKETS is not supported
  if(loc === 'ALL'){
    E('tb-loc').textContent = 'All Markets';
    E('s-loc').textContent  = 'Select a specific location';
    E('s-crit').textContent = '—';
    E('s-avail').textContent= '—';
    E('s-noalt').textContent= '—';
    E('s-total').textContent= '—';
    if(E('s-nodata')) E('s-nodata').textContent = '—';
    E('content').innerHTML  =
      '<div style="margin:40px auto;max-width:480px;text-align:center;">'
      +'<div style="font-size:2rem;margin-bottom:14px;">&#127760;</div>'
      +'<div style="font-size:0.9rem;font-weight:700;color:var(--white);margin-bottom:8px;">Select a Marketplace</div>'
      +'<div style="font-size:0.78rem;color:var(--text3);line-height:1.6;">'
      +'Remap suggestions are location-specific.<br>'
      +'Please select <strong style="color:var(--sky);">United Kingdom</strong>, '
      +'<strong style="color:var(--sky);">United States</strong>, or '
      +'<strong style="color:var(--sky);">Germany</strong> from the sidebar to view remap suggestions.'
      +'</div></div>';
    return;
  }

  E('tb-loc').textContent = loc;
  E('s-loc').textContent  = loc+' marketplace';
  loadData();
}

// ── Metric filter ─────────────────────────────────────────────────────────────
function setFilter(val){
  activeFilter=val;
  E('sf').value=(val==='CRITICAL_ZERO'||val==='NO_DATA_ZERO'?'':val);
  document.querySelectorAll('.metric').forEach(function(m){m.classList.remove('active');});
  var id = val==='CRITICAL_ZERO' ? 'mc-crit'
         : val==='NO_DATA_ZERO'  ? 'mc-nodata'
         : val==='REMAP_AVAILABLE'? 'mc-avail'
         : val==='NO_ALTERNATIVE' ? 'mc-noalt'
         : 'mc-total';
  var el=E(id); if(el) el.classList.add('active');
  applyFilters();
}
function onStatusDrop(){
  activeFilter=E('sf').value;
  document.querySelectorAll('.metric').forEach(function(m){m.classList.remove('active');});
  applyFilters();
}

// ── Holder sidebar ────────────────────────────────────────────────────────────
function selectHolder(btn){
  activeHolder=btn.dataset.h;
  document.querySelectorAll('.hbtn-sb').forEach(function(b){b.classList.toggle('active',b===btn);});
  applyFilters();
}

// ── Background location counts ────────────────────────────────────────────────
function loadLocCounts(){
  // ONE lightweight call instead of 3 expensive 7-CTE queries
  // Returns real synced_at from dashboard_cache for stale detection
  fetch('/api/remap-summary')
    .then(function(r){return r.json();})
    .then(function(d){
      ['UK','US','Germany'].forEach(function(loc){
        var info = d[loc] || {};
        var crit = info.critical || 0;
        if(E('mc-'+loc)) E('mc-'+loc).textContent = info.total || 0;
        var critEl = E('mc-'+loc+'-c');
        if(critEl){
          if(crit > 0){ critEl.textContent=crit; critEl.style.display=''; }
          else { critEl.style.display='none'; }
        }
      });

      // ── Stale cache check ───────────────────────────────────────────────────
      // d.updated_at = real synced_at from dashboard_cache (db_sync.py run time)
      // If server is newer than our cached data → silently re-fetch
      var serverSyncTs = d.updated_at || '';
      if(serverSyncTs && _cachedRemap && _isRemapCacheStale(serverSyncTs)){
        console.log('Remap cache stale — server synced at', serverSyncTs, '— refreshing');
        // Clear cache so loadData() fetches fresh data
        try{
          sessionStorage.removeItem(REMAP_CACHE_KEY);
          sessionStorage.removeItem(REMAP_SYNC_KEY);
        }catch(e){}
        _cachedRemap = null;
        // Re-fetch silently — user sees current rows until new data arrives
        fetch('/api/remap-suggestions?location='+encodeURIComponent(activeLoc))
          .then(function(r){return r.json();})
          .then(function(fresh){
            if(fresh.error) return;
            allRows = fresh.results || [];
            _saveRemapCache(allRows, activeLoc, serverSyncTs);
            applyFilters(); // re-render with fresh data
          }).catch(function(){});
      } else if(serverSyncTs && !_getCachedRemapSyncTs() && _cachedRemap){
        // Cache exists but has no sync_ts yet — save the server ts now
        sessionStorage.setItem(REMAP_SYNC_KEY, serverSyncTs);
      }
    }).catch(function(){});
}

// ── Load data ─────────────────────────────────────────────────────────────────
async function loadData(){
  var _sk='';for(var _i=0;_i<8;_i++)_sk+='<div class="skeleton-card"></div>';
  E('content').innerHTML='<div class="loader-wrap"><div class="loader-ring"></div>'
    +'<div class="loader-label">Fetching remap data…</div></div>'+_sk;
  try{
    var d=await (await fetch('/api/remap-suggestions?location='+encodeURIComponent(activeLoc))).json();
    if(d.error){E('content').innerHTML='<div class="loading-msg" style="color:var(--red);">⚠ Error: '+esc(d.error)+'</div>';return;}
    allRows=d.results||[];
    allRows.sort(function(a,b){
      if(a.current_stock!==b.current_stock) return a.current_stock-b.current_stock;
      return (a.holder_name||'').localeCompare(b.holder_name||'');
    });

    var critCount  =allRows.filter(function(r){return r.dashboard_status==='CRITICAL';}).length;
    var nodataCount=allRows.filter(function(r){return r.dashboard_status==='NO DATA'&&r.current_stock<=5;}).length;
    var avail=allRows.filter(function(r){return r.remap_status==='REMAP_AVAILABLE';}).length;
    var noalt=allRows.filter(function(r){return r.remap_status==='NO_ALTERNATIVE';}).length;
    E('s-crit').textContent=critCount;
    E('s-avail').textContent=avail;
    E('s-noalt').textContent=noalt;
    E('s-total').textContent=d.total;
    if(E('s-nodata')) E('s-nodata').textContent=nodataCount;
    E('ts').textContent=new Date(d.updated_at).toLocaleTimeString();

    // Build holder sidebar
    var holderMap={};
    allRows.forEach(function(r){
      var h=r.holder_name||'?';
      if(!holderMap[h]) holderMap[h]={total:0,crit:0};
      holderMap[h].total++;
      if(r.dashboard_status==='CRITICAL') holderMap[h].crit++;
      if(r.dashboard_status==='NO DATA')   holderMap[h].nodata = (holderMap[h].nodata||0)+1;
    });
    var holders=Object.keys(holderMap).sort();
    var html='<button class="hbtn-sb'+(activeHolder==='ALL'?' active':'')+'" data-h="ALL" onclick="selectHolder(this)">'
      +'<span class="hb-dot"></span><span class="hb-name">All Holders</span>'
      +'<span class="hb-cnt">'+allRows.length+'</span></button>';
    holders.forEach(function(h){
      var info=holderMap[h];
      var critBadge  = info.crit>0   ? '<span class="hb-crit">'+info.crit+'</span>' : '';
      html+='<button class="hbtn-sb'+(activeHolder===h?' active':'')+'" data-h="'+esc(h)+'" onclick="selectHolder(this)">'
        +'<span class="hb-dot"></span>'
        +'<span class="hb-name" title="'+esc(h)+'">'+esc(h)+'</span>'
        +critBadge
        +'<span class="hb-cnt">'+info.total+'</span>'
        +'</button>';
    });
    E('sb-holders').innerHTML=html;

    _saveRemapCache(allRows, activeLoc, d.updated_at || ''); // persist with server sync time
    applyFilters();
  }catch(err){
    E('content').innerHTML='<div class="loading-msg" style="color:var(--red);">&#9888; Failed to load data. <button onclick="loadData()" style="background:var(--card2);border:1px solid var(--line2);color:var(--text2);border-radius:5px;padding:3px 10px;font-size:0.7rem;cursor:pointer;margin-left:6px;">Retry</button></div>';
  }
}

// ── Filters ───────────────────────────────────────────────────────────────────
function applyFilters(){
  var q=E('qbox').value.trim().toLowerCase();
  var sf=E('sf').value||(activeFilter==='CRITICAL_ZERO'?'':activeFilter);
  var rows=allRows;
  if(activeHolder&&activeHolder!=='ALL') rows=rows.filter(function(r){return r.holder_name===activeHolder;});
  if(activeFilter==='CRITICAL_ZERO')
    rows=rows.filter(function(r){return r.dashboard_status==='CRITICAL';});
  else if(activeFilter==='NO_DATA_ZERO')
    rows=rows.filter(function(r){return r.dashboard_status==='NO DATA';});
  // NO DATA includes stock<=5 per OUT_OF_STOCK_THRESHOLD
  else if(sf) rows=rows.filter(function(r){return r.remap_status===sf;});
  if(q) rows=rows.filter(function(r){
    return r.out_of_stock_sku.toLowerCase().indexOf(q)>=0
        ||(r.product_name||'').toLowerCase().indexOf(q)>=0
        ||(r.holder_name||'').toLowerCase().indexOf(q)>=0
        ||(r.suggested_sku||'').toLowerCase().indexOf(q)>=0;
  });
  _filtered=rows;
  renderContent(rows);
}

// ── Render ────────────────────────────────────────────────────────────────────
function renderContent(rows){
  var rc=rows.length.toLocaleString()+' records \u00b7 '+activeLoc;
  if(!rows.length){
    E('content').innerHTML='<div class="rc">'+rc+'</div><div class="empty-msg">No results matching the current filters.</div>';
    return;
  }
  rows.forEach(function(r,i){r._idx=i;});

  // ── Split using dashboard_server.py status logic (PRIORITY 1) ──────────────
  // dashboard_status = 'CRITICAL' → days_remaining <= 7 OR stock=0 with sales
  // dashboard_status = 'NO DATA'  → stock=0, no sales history (also needs remap)
  // LOW is NOT included — business rule: only CRITICAL needs remap
  var critAvail = rows.filter(function(r){
    return r.dashboard_status==='CRITICAL' && r.remap_status==='REMAP_AVAILABLE';
  });
  var critNoAlt = rows.filter(function(r){
    return r.dashboard_status==='CRITICAL' && r.remap_status==='NO_ALTERNATIVE';
  });
  var nodataAvail = rows.filter(function(r){
    return r.dashboard_status==='NO DATA' && r.current_stock<=5 && r.remap_status==='REMAP_AVAILABLE';
  });
  var nodataNoAlt = rows.filter(function(r){
    return r.dashboard_status==='NO DATA' && r.current_stock<=5 && r.remap_status==='NO_ALTERNATIVE';
  });

  var html='<div class="rc">'+rc+'</div>';

  // CRITICAL section first — highest priority
  if(critAvail.length)    html += mkSection(
    'CRITICAL \u2014 Days Left \u22647, Remap Available',
    'sb-crit', critAvail, 'crit');
  if(critNoAlt.length)    html += mkSection(
    'CRITICAL \u2014 Days Left \u22647, No Alternative',
    'sb-crit', critNoAlt, 'crit');

  // NO DATA with zero stock — genuinely empty, no sales history
  if(nodataAvail.length)  html += mkSection(
    'NO DATA \u2014 Stock \u22645, No Sales History, Remap Available',
    'sb-nodata', nodataAvail, 'nodata');
  if(nodataNoAlt.length)  html += mkSection(
    'NO DATA \u2014 Stock \u22645, No Sales History, No Alternative',
    'sb-nodata', nodataNoAlt, 'nodata');

  E('content').innerHTML=html;
  E('content').classList.remove('fade-in-up'); void E('content').offsetWidth; E('content').classList.add('fade-in-up');
}

function mkSection(label, cls, rows, stype){
  var isCrit   = (stype === 'crit');
  var isNoData = (stype === 'nodata');
  var colHdr='<div class="col-hdr">'
    +'<span class="ch">OOS SKU</span>'
    +'<span class="ch">Product Name</span>'
    +'<span class="ch center">Stock</span>'
    +'<span class="ch">Suggested SKU</span>'
    +'<span class="ch center">Avail.</span>'
    +'<span class="ch">Status</span>'
    +'<span class="ch">Remap</span>'
    +'<span class="ch">Holder</span>'
    +'<span class="ch center">Action</span>'
    +'</div>';
  var cards=rows.map(function(r){
    var idx = r._idx;
    var pcCls = isCrit   ? 'prod-card pc-crit'
              : isNoData ? 'prod-card pc-nodata'
              : 'prod-card pc-low';

    // Stock indicator — show actual stock with dashboard status colour
    var stEl;
    if(r.current_stock === 0){
      stEl = '<span class="st-zero">0</span>';
    } else if(isCrit){
      stEl = '<span class="st-crit-low">'+r.current_stock+'</span>';
    } else {
      stEl = '<span class="st-low">'+r.current_stock+'</span>';
    }

    // Avg/day from dashboard velocity
    var avgEl = (r.avg_per_day && r.avg_per_day > 0)
      ? '<span style="font-family:\'JetBrains Mono\',monospace;font-size:0.7rem;color:var(--sky);">'+r.avg_per_day+'</span>'
      : '<span style="color:var(--text3);font-size:0.7rem;">\u2014</span>';

    var sskuHtml = r.suggested_sku
      ? '<span class="pc-ssku">'+esc(r.suggested_sku)+'</span>'
      : '<span style="color:var(--text3);font-size:0.7rem;">No alternative</span>';
    var avHtml = r.suggested_stock > 0
      ? '<span class="av-pill">'+r.suggested_stock+'</span>'
      : '<span style="color:var(--text3);">\u2014</span>';

    // Dashboard status badge — shows exact dashboard_server.py classification
    var dsBadge = isCrit
      ? '<span style="background:rgba(220,38,38,.12);color:var(--red);border:1px solid rgba(220,38,38,.25);border-radius:4px;padding:2px 7px;font-size:0.58rem;font-weight:700;text-transform:uppercase;">CRITICAL</span>'
      : '<span style="background:rgba(107,114,128,.1);color:var(--text3);border:1px solid rgba(107,114,128,.2);border-radius:4px;padding:2px 7px;font-size:0.58rem;font-weight:700;text-transform:uppercase;">NO DATA</span>';

    return '<div class="'+pcCls+'">'
      +'<div class="prod-col"><span class="pc-sku">'+esc(r.out_of_stock_sku)+'</span></div>'
      +'<div class="prod-col pc-name" title="'+esc(r.product_name||'')+'">'+esc(r.product_name||r.out_of_stock_sku)+'</div>'
      +'<div class="prod-col pc-stock" style="text-align:center;">'+stEl+'</div>'
      +'<div class="prod-col">'+sskuHtml+'</div>'
      +'<div class="prod-col pc-avail" style="text-align:center;">'+avHtml+'</div>'
      +'<div class="prod-col">'+dsBadge+'</div>'
      +'<div class="prod-col"><span class="stag '+r.remap_status+'">'+r.remap_status.replace('_',' ')+'</span></div>'
      +'<div class="prod-col"><span class="holder-tag" onclick="goHolder(this)">'+esc(r.holder_name||'')+'</span></div>'
      +'<div class="prod-col pc-action"><button class="det-btn" onclick="goDetail(\''+esc(r.out_of_stock_sku)+'\',\''+esc(r.suggested_sku||'')+'\')">Details</button></div>'
      +'</div>';
  }).join('');
  return '<div class="sec-mb">'
    +'<div class="sec-hdr">'
      +'<span class="sec-badge '+cls+'"><span class="sb-pulse"></span>'+label+'</span>'
      +'<span class="sec-count">'+rows.length+' items</span>'
      +'<span class="sec-line"></span>'
    +'</div>'
    +'<div class="tbl-scroll"><div class="tbl-inner">'
    +colHdr
    +'<div class="prod-list">'+cards+'</div>'
    +'</div></div>'
    +'</div>';
}

// ── Navigate to detail card (same tab) ───────────────────────────────────────
function goDetail(sku, compareSku){
  _saveRemapState(); // save state before navigating away
  var url = '/product-detail-card?sku='+encodeURIComponent(sku)
    +'&location='+encodeURIComponent(activeLoc)
    +'&back='+encodeURIComponent('/remap');
  if(compareSku) url += '&compare='+encodeURIComponent(compareSku);
  window.location.href = url;
}

// ── Holder redirect ───────────────────────────────────────────────────────────
function goHolder(el){
  var h=el.textContent.trim();
  if(h) window.open('/?holder='+encodeURIComponent(h),'_blank');
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function openModal(idx){
  var r=_filtered[idx];
  if(!r){return;}
  E('m-title').textContent=r.out_of_stock_sku;
  E('m-sub').textContent=r.product_name||'Product comparison & action checklist';
  E('m-strip').innerHTML=ms('Holder',r.holder_name)+ms('Location',activeLoc)+ms('Stock',r.current_stock+' units')+ms('Parent',r.parent_sku||'\u2014')+ms('Status',r.remap_status.replace('_',' '));
  // Use activeLoc to build correct marketplace link
  var _amzDomain = activeLoc==='Germany'?'amazon.de':activeLoc==='US'?'amazon.com':'amazon.co.uk';
  var cu=r.current_asin?'https://www.'+_amzDomain+'/dp/'+r.current_asin:'#';
  var su=r.suggested_asin?'https://www.'+_amzDomain+'/dp/'+r.suggested_asin:'#';
  E('lnk-cur').href=cu; E('lnk-cur').style.opacity=r.current_asin?'1':'0.4';
  E('lnk-sug').href=su; E('lnk-sug').style.opacity=(r.suggested_asin&&r.suggested_sku)?'1':'0.4';

  // Render base comparison immediately (no loading delay)
  E('m-cmp').innerHTML=
    cmpCard('cur','&#9888; Current Listing','cmpcard cur','cmpcard-hdr',r.out_of_stock_sku,r.current_asin,r.product_name,r.current_color,r.current_stock)
    +(r.suggested_sku
      ?cmpCard('sug','&#10003; Suggested Replacement','cmpcard sug','cmpcard-hdr',r.suggested_sku,r.suggested_asin,r.suggested_name,r.suggested_color,r.suggested_stock)
      :'<div class="cmpcard sug"><div class="cmpcard-hdr">&#10003; Suggested Replacement</div>'
       +'<div class="cmpcard-body" style="align-items:center;justify-content:center;min-height:100px;color:var(--text3);font-size:0.75rem;">No alternative found in '+activeLoc+'</div></div>');

  E('m-map').innerHTML=mf('Location',activeLoc)+mf('Parent SKU',r.parent_sku||'\u2014')+mf('Family','Same family')+mf('OOS Rule','Stock \u2264 5 units')+mf('Alt Rule','Stock > 10 units')+mf('Selection','Highest stock variant');

  // Show loading state for enriched sections
  E('m-detail-cur').innerHTML='<div class="det-loading">Loading product details&hellip;</div>';
  E('m-detail-sug').innerHTML=r.suggested_sku?'<div class="det-loading">Loading suggestion details&hellip;</div>':'';

  E('modal-overlay').classList.add('open');
  document.body.style.overflow='hidden';

  // Fetch product detail for current SKU (non-blocking)
  fetchDetail(r.out_of_stock_sku, 'amazon', E('m-detail-cur'));

  // Fetch product detail for suggested SKU if exists
  if(r.suggested_sku){
    fetchDetail(r.suggested_sku, 'amazon', E('m-detail-sug'));
  }
}

function fetchDetail(sku, channel, container){
  fetch('/api/product-detail?sku='+encodeURIComponent(sku)+'&location='+encodeURIComponent(activeLoc))
    .then(function(res){return res.json();})
    .then(function(d){
      if(!container) return;
      if(d.error){
        container.innerHTML='<div class="det-loading" style="color:var(--text3);">No listing detail found for this SKU.</div>';
        return;
      }
      var html='';

      // ── Image + Title side by side ──
      html+='<div style="display:flex;gap:14px;margin-bottom:14px;align-items:flex-start;">';

      // Image
      if(d.image_url){
        html+='<div style="flex-shrink:0;">'
          +'<img src="'+esc(d.image_url)+'" alt="'+esc(sku)+'" '
          +'style="width:120px;height:120px;object-fit:contain;border-radius:8px;'
          +'background:rgba(255,255,255,.04);border:1px solid var(--line2);padding:4px;" '
          +'onerror="this.parentElement.style.display=\'none\'">'
          +'</div>';
      }

      // Meta block
      html+='<div style="flex:1;min-width:0;">';
      if(d.title){
        html+='<div style="font-size:0.82rem;font-weight:600;color:var(--white);line-height:1.4;margin-bottom:8px;">'+esc(d.title)+'</div>';
      }
      html+='<div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px;">';
      if(d.price && d.price > 0){
        html+='<span style="background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.25);color:var(--amber);border-radius:5px;padding:2px 9px;font-size:0.7rem;font-weight:700;font-family:\'JetBrains Mono\',monospace;">'+esc(d.currency)+' '+parseFloat(d.price).toFixed(2)+'</span>';
      }
      if(d.status){
        var scol=d.status.toLowerCase()==='active'?'var(--emerald)':'var(--text3)';
        html+='<span style="font-size:0.68rem;color:'+scol+';">'+esc(d.status)+'</span>';
      }
      if(d.channel){
        html+='<span style="font-size:0.68rem;color:var(--sky);text-transform:capitalize;">'+esc(d.channel)+'</span>';
      }
      html+='</div>';

      // Variations
      if(d.variations && d.variations.length > 0){
        html+='<div style="display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px;">';
        d.variations.forEach(function(v){
          html+='<span style="background:rgba(56,189,248,.08);border:1px solid rgba(56,189,248,.15);color:var(--sky);border-radius:5px;padding:2px 8px;font-size:0.65rem;">'
            +'<span style="color:var(--text3);">'+esc(v.name)+':</span> '+esc(v.value)+'</span>';
        });
        html+='</div>';
      }
      html+='</div>';
      html+='</div>';

      // ── Description ──
      if(d.description){
        html+='<div style="background:var(--card);border:1px solid var(--line2);border-radius:7px;padding:10px 12px;margin-bottom:10px;">'
          +'<div style="font-size:0.58rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text3);margin-bottom:6px;">Product Description</div>'
          +'<div style="font-size:0.74rem;color:var(--text2);line-height:1.6;white-space:pre-wrap;">'+esc(d.description)+'</div>'
          +'</div>';
      }

      // ── Listing link ──
      if(d.listing_url){
        html+='<div style="margin-top:4px;">'
          +'<a href="'+esc(d.listing_url)+'" target="_blank" style="font-size:0.7rem;color:var(--sky);text-decoration:none;">&#128279; View listing: '+esc(d.listing_url.slice(0,60))+'&hellip;</a>'
          +'</div>';
      }

      container.innerHTML = html || '<div class="det-loading" style="color:var(--text3);">No detail available.</div>';
    })
    .catch(function(){
      if(container) container.innerHTML='<div class="det-loading" style="color:var(--text3);">Could not load product detail.</div>';
    });
}

// ── URL param: handle ?sku=X&location=UK from dashboard remap button ──────────
function checkUrlSku(){
  var params = new URLSearchParams(window.location.search);
  var sku      = params.get('sku');
  var locParam = params.get('location');

  if(!sku) return;

  // If a location was passed, switch to it first
  if(locParam && locParam !== activeLoc && ['UK','US','Germany'].indexOf(locParam) >= 0){
    activeLoc = locParam;
    E('tb-loc').textContent = locParam;
    E('s-loc').textContent  = locParam + ' marketplace';
    // Update sidebar active button
    document.querySelectorAll('.sb-mkt-btn').forEach(function(b){
      b.classList.toggle('active', b.dataset.loc === locParam);
    });
    // Reload data for the new location then search
    loadData().then(function(){ _findAndHighlight(sku); }).catch(function(){});
    return;
  }

  _findAndHighlight(sku);
}

function _findAndHighlight(sku){
  // Scroll to and highlight the matching row, auto-search if not visible
  var idx = _filtered.findIndex(function(r){
    return r.out_of_stock_sku === sku;
  });
  if(idx >= 0){
    // Put the sku in the search box so it's visible
    E('qbox').value = sku;
    applyFilters();
    // Re-find after filter
    idx = _filtered.findIndex(function(r){ return r.out_of_stock_sku === sku; });
    if(idx >= 0){
      // Scroll the matching card into view
      setTimeout(function(){
        var cards = E('content').querySelectorAll('.prod-card');
        if(cards[idx]){
          cards[idx].scrollIntoView({behavior:'smooth', block:'center'});
          cards[idx].style.outline = '2px solid var(--sky)';
          cards[idx].style.outlineOffset = '2px';
          setTimeout(function(){ cards[idx].style.outline=''; }, 2000);
        }
      }, 100);
    }
  } else {
    // SKU not in OOS list — put in search box so user sees it
    E('qbox').value = sku;
    applyFilters();
  }
}


function cmpCard(type,title,cardCls,hdrCls,sku,asin,name,color,stock){
  var _amzBase='https://www.'+(activeLoc==='Germany'?'amazon.de':activeLoc==='US'?'amazon.com':'amazon.co.uk')+'/dp/';
  var asinHtml=asin?'<a class="alink" href="'+_amzBase+esc(asin)+'" target="_blank">'+esc(asin)+' &#8599;</a>':'<span class="cfv na">\u2014</span>';
  return '<div class="'+cardCls+'"><div class="'+hdrCls+'">'+title+'</div><div class="cmpcard-body">'
    +cf('SKU',sku,true)+cf2('ASIN',asinHtml)+cf('Product Name',name)+cf('Color/Variant',color||'Not specified')+cf('Stock ('+activeLoc+')',(stock||0)+' units')
    +'</div></div>';
}
function cf(l,v,mono){var cls=v?(mono?'cfv mono':'cfv'):'cfv na';return '<div class="cfield"><div class="cfl">'+l+'</div><div class="'+cls+'">'+esc(v||'\u2014')+'</div></div>';}
function cf2(l,html){return '<div class="cfield"><div class="cfl">'+l+'</div><div class="cfv">'+html+'</div></div>';}
function ms(l,v){return '<div class="ms-it">'+l+': <strong>'+esc(String(v||'\u2014'))+'</strong></div>';}
function mf(l,v){return '<div class="mfield"><div class="mfl">'+l+'</div><div class="mfv">'+esc(v||'\u2014')+'</div></div>';}

function closeModal(e){if(e.target===E('modal-overlay'))closeModalDirect();}
function closeModalDirect(){E('modal-overlay').classList.remove('open');document.body.style.overflow='';}
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeModalDirect();});

// ── Init — smart cache-aware startup ─────────────────────────────────────────
// If coming back from Product Details page: restore state from sessionStorage
// If fresh load: fetch from API as normal
loadLocCounts();

var _cachedRemap = _loadRemapCache();
var _savedState  = _loadRemapState();

if(_cachedRemap && _cachedRemap.loc === activeLoc){
  // Restore from sessionStorage — instant, no API call
  allRows = _cachedRemap.rows || [];

  // Restore UI state
  if(_savedState){
    activeLoc     = _savedState.activeLoc     || activeLoc;
    activeHolder  = _savedState.activeHolder  || 'ALL';
    activeFilter  = _savedState.activeFilter  || '';
    if(_savedState.qbox && E('qbox')) E('qbox').value = _savedState.qbox;
    if(_savedState.sf   && E('sf'))   E('sf').value   = _savedState.sf;
    // Update sidebar location button
    document.querySelectorAll('.sb-mkt-btn').forEach(function(b){
      b.classList.toggle('active', b.dataset.loc === activeLoc);
    });
    E('tb-loc').textContent = activeLoc;
    E('s-loc').textContent  = activeLoc+' marketplace';
  }

  // Update metrics + rebuild holder sidebar from cached data
  var critCount   = allRows.filter(function(r){return r.dashboard_status==='CRITICAL';}).length;
  var nodataCount = allRows.filter(function(r){return r.dashboard_status==='NO DATA'&&r.current_stock<=5;}).length;
  var avail = allRows.filter(function(r){return r.remap_status==='REMAP_AVAILABLE';}).length;
  var noalt = allRows.filter(function(r){return r.remap_status==='NO_ALTERNATIVE';}).length;
  E('s-crit').textContent  = critCount;
  E('s-avail').textContent = avail;
  E('s-noalt').textContent = noalt;
  E('s-total').textContent = allRows.length;
  if(E('s-nodata')) E('s-nodata').textContent = nodataCount;

  // Rebuild holder sidebar
  var holderMap={};
  allRows.forEach(function(r){
    var h=r.holder_name||'?';
    if(!holderMap[h]) holderMap[h]={total:0,crit:0};
    holderMap[h].total++;
    if(r.dashboard_status==='CRITICAL') holderMap[h].crit++;
  });
  var holders=Object.keys(holderMap).sort();
  var hhtml='<button class="hbtn-sb'+(activeHolder==='ALL'?' active':'')+'" data-h="ALL" onclick="selectHolder(this)">'
    +'<span class="hb-dot"></span><span class="hb-name">All Holders</span>'
    +'<span class="hb-cnt">'+allRows.length+'</span></button>';
  holders.forEach(function(h){
    var info=holderMap[h];
    var critBadge=info.crit>0?'<span class="hb-crit">'+info.crit+'</span>':'';
    hhtml+='<button class="hbtn-sb'+(activeHolder===h?' active':'')+'" data-h="'+h.replace(/"/g,'&quot;')+'" onclick="selectHolder(this)">'
      +'<span class="hb-dot"></span><span class="hb-name">'+h.replace(/</g,'&lt;')+'</span>'
      +critBadge+'<span class="hb-cnt">'+info.total+'</span></button>';
  });
  E('sb-holders').innerHTML = hhtml;

  applyFilters();
  checkUrlSku();

  // Restore scroll position after rows are rendered (GAP-04)
  if(_savedState && _savedState.scroll){
    requestAnimationFrame(function(){
      var _c = E('content');
      if(_c) _c.scrollTop = _savedState.scroll;
    });
  }
} else {
  // Fresh load — fetch from API
  loadData().then(function(){ checkUrlSku(); }).catch(function(){ checkUrlSku(); });
}

</script>
</body>
</html>"""