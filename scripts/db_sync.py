#!/usr/bin/env python3
"""
LEDs One — db_sync.py
MySQL (remote 149.28.134.54:3307) → PostgreSQL (local stock_level db) sync.
Runs every 20 minutes via OpenClaw cron.
Logs: /opt/openclaw/stock_level/logs/db_sync.log

FIXED:
- Stock from centralizer.location_wise_inv_stock (not order_management.inv_final_stock)
- Order items use correct column oii_item_sku (not oii_itm_real_sku)
- PH mapping synced from MySQL ph_cate_products (not Excel file)
- Product info only from ebay_products with is_deleted=0, is_ended=0
"""
import logging
import os
import sys
import time
from datetime import datetime, timedelta

import pymysql
import psycopg2
from psycopg2.extras import execute_values

LOG_PATH = "/opt/openclaw/stock_level/logs/db_sync.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [db_sync] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("db_sync")

MYSQL_CONFIG = {
    "host":            "149.28.134.54",
    "port":            3307,
    "user":            "ledsone-db-system-user",
    "password":        "r4315cgklqsj",
    "connect_timeout": 30,
    "charset":         "utf8mb4",
}

PG_CONFIG = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "stock_level",
    "user":     "digit_web",
    "password": "digit123",
}

def mysql_conn(database):
    return pymysql.connect(**MYSQL_CONFIG, database=database)

def pg_conn():
    return psycopg2.connect(**PG_CONFIG)

def log_step(label, rows, elapsed):
    log.info(f"  {label}: {rows:,} rows in {elapsed:.1f}s")

DDL = """
-- Stock from centralizer (correct source — replaces old inv_final_stock)
CREATE TABLE IF NOT EXISTS location_wise_inv_stock (
    id           SERIAL PRIMARY KEY,
    sku          TEXT,
    location     TEXT,
    stock        INTEGER DEFAULT 0,
    synced_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Orders (last 30 days)
CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    internal_id BIGINT UNIQUE,
    order_id    TEXT,
    order_date  DATE,
    status      TEXT,
    channel     TEXT,
    synced_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Order items — uses oii_item_sku (correct column)
CREATE TABLE IF NOT EXISTS order_item_info (
    id        SERIAL PRIMARY KEY,
    order_id  BIGINT,
    sku       TEXT,
    quantity  INTEGER DEFAULT 0,
    synced_at TIMESTAMPTZ DEFAULT NOW()
);

-- Product listings (active only, no duplicates)
-- Extended to include detail fields: image, price, description, listing_url
-- One row per effective_sku — site-independent — used for all counting and filtering
-- effective_sku = mapped_sku if exists, else original sku (fallback for NULL mapped_sku)
-- sku column added: stores original listing sku for NULL mapped_sku products
CREATE TABLE IF NOT EXISTS ebay_products (
    mapped_sku          TEXT,
    sku                 TEXT,
    title               TEXT,
    which_channel       TEXT,
    item_id             TEXT,
    main_image_url      TEXT,
    price               NUMERIC(10,2),
    currency            TEXT,
    listing_url         TEXT,
    status              TEXT,
    product_description TEXT,
    selected_variations TEXT,
    synced_at           TIMESTAMPTZ DEFAULT NOW()
);

-- Site-specific titles — one row per (mapped_sku, site) for title display only
-- Also stores item_id (ASIN) and listing_url per site because:
--   3,250 SKUs have DIFFERENT ASINs per marketplace (UK/Germany/US)
--   listing_url domain is site-specific (amazon.co.uk / amazon.de / amazon.com)
-- LEFT JOINed by dashboard to show UK/Germany/US-specific data when available
-- Does NOT affect product counts or filtering — fallback to ebay_products when missing
CREATE TABLE IF NOT EXISTS ebay_product_titles (
    mapped_sku   TEXT,
    site         TEXT,
    title        TEXT,
    item_id      TEXT,
    listing_url  TEXT,
    synced_at    TIMESTAMPTZ DEFAULT NOW()
);

-- PH holder mapping from MySQL (replaces Excel-based mapping)
CREATE TABLE IF NOT EXISTS ph_mapping (
    mapped_sku  TEXT PRIMARY KEY,
    holder_name TEXT,
    synced_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Manual overrides (inbound units, reorder points)
CREATE TABLE IF NOT EXISTS sku_extras (
    sku           TEXT PRIMARY KEY,
    inbound_units INTEGER DEFAULT 0,
    reorder_point INTEGER DEFAULT 0,
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Amazon variant grouping — maps mapped_sku to parent_sku for remap logic
CREATE TABLE IF NOT EXISTS amazon_variants (
    mapped_sku  TEXT,
    parent_sku  TEXT,
    asin        TEXT,
    color       TEXT,
    synced_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_av_mapped_sku ON amazon_variants(mapped_sku);
CREATE INDEX IF NOT EXISTS idx_av_parent_sku ON amazon_variants(parent_sku);

-- Bullet points per product (linked via ebay_products.id = product_id in MySQL)
CREATE TABLE IF NOT EXISTS product_bullet_points (
    mapped_sku  TEXT,
    point_text  TEXT,
    view_order  INTEGER DEFAULT 0,
    synced_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bp_mapped_sku ON product_bullet_points(mapped_sku);

-- Sub images per product (linked via ebay_products.id = product_id in MySQL)
CREATE TABLE IF NOT EXISTS product_sub_images (
    mapped_sku  TEXT,
    image_url   TEXT,
    view_order  INTEGER DEFAULT 0,
    synced_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_si_mapped_sku ON product_sub_images(mapped_sku);

-- Pre-computed dashboard cache — built at sync time, served directly to browser.
-- Replaces the live 5-CTE PostgreSQL query that ran on every API request.
-- One row per (sku, location) for UK / US / Germany.
-- Also one row per sku with location='ALL' (sum of all locations).
-- Updated every 20 minutes by sync_dashboard_cache().
-- API /api/stock?marketplace=UK → SELECT * FROM dashboard_cache WHERE location='UK'
-- Response time: ~200ms instead of ~2-3s (no computation at request time).
CREATE TABLE IF NOT EXISTS dashboard_cache (
    sku             TEXT,
    location        TEXT,
    product_name    TEXT,
    platform        TEXT,
    item_id         TEXT,
    stock           INTEGER DEFAULT 0,
    inbound         INTEGER DEFAULT 0,
    reorder_point   INTEGER DEFAULT 0,
    sold_7d         INTEGER DEFAULT 0,
    sold_14d        INTEGER DEFAULT 0,
    sold_30d        INTEGER DEFAULT 0,
    avg_per_day     NUMERIC(8,2) DEFAULT 0,
    days_remaining  INTEGER,
    status          TEXT,
    holder          TEXT,
    sort_order      SMALLINT DEFAULT 4,
    synced_at       TIMESTAMPTZ DEFAULT NOW()
);
-- Composite index on (location, sort_order, sku) — covers the full query:
-- WHERE location = %s ORDER BY sort_order, sku
-- PostgreSQL can use this index for both filtering AND sorting — no in-memory sort
CREATE INDEX IF NOT EXISTS idx_dc_loc_sort ON dashboard_cache(location, sort_order, sku);
CREATE INDEX IF NOT EXISTS idx_dc_location ON dashboard_cache(location);
CREATE INDEX IF NOT EXISTS idx_dc_sku      ON dashboard_cache(sku);
"""

def ensure_tables(pg):
    with pg.cursor() as cur:
        cur.execute(DDL)
    pg.commit()
    log.info("PostgreSQL tables verified / created.")


def sync_location_wise_inv_stock(pg):
    """
    FIXED: Fetch from centralizer.location_wise_inv_stock
    (was incorrectly using order_management.inv_final_stock)
    """
    log.info("Syncing location_wise_inv_stock from centralizer ...")
    t0 = time.time()
    with mysql_conn("centralizer") as my:
        with my.cursor() as cur:
            cur.execute("SELECT TRIM(sku), location, stock FROM location_wise_inv_stock")
            rows = cur.fetchall()
    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE location_wise_inv_stock RESTART IDENTITY")
        if rows:
            execute_values(
                cur,
                "INSERT INTO location_wise_inv_stock (sku, location, stock) VALUES %s",
                rows,
            )
    pg.commit()
    log_step("location_wise_inv_stock", len(rows), time.time() - t0)


def sync_orders(pg):
    """Sync last 30 days of orders."""
    log.info("Syncing orders (last 30 days) ...")
    t0 = time.time()
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    with mysql_conn("order_management") as my:
        with my.cursor() as cur:
            cur.execute("""
                SELECT `order`,
                       order_id,
                       DATE(order_date) AS order_date,
                       order_status,
                       order_market_place
                FROM   `order`
                WHERE  order_date >= %s
            """, (cutoff,))
            rows = cur.fetchall()
    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE orders RESTART IDENTITY")
        if rows:
            execute_values(
                cur,
                """INSERT INTO orders (internal_id, order_id, order_date, status, channel)
                   VALUES %s ON CONFLICT DO NOTHING""",
                rows,
            )
    pg.commit()
    log_step("orders", len(rows), time.time() - t0)


def sync_order_item_info(pg):
    """
    FIXED: Use correct column names oii_item_sku and oii_item_quantity
    (was using oii_itm_real_sku and oii_itm_real_qty which don't exist)
    """
    log.info("Syncing order_item_info (last 30 days) ...")
    t0 = time.time()
    cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    with mysql_conn("order_management") as my:
        with my.cursor() as cur:
            cur.execute("""
                SELECT oii.oii_order_id,
                       TRIM(oii.oii_item_sku),
                       COALESCE(CAST(oii.oii_item_quantity AS SIGNED), 0)
                FROM   order_item_info oii
                JOIN   `order` o ON o.`order` = oii.oii_order_id
                WHERE  o.order_date >= %s
                  AND  TRIM(oii.oii_item_sku) IS NOT NULL
                  AND  TRIM(oii.oii_item_sku) != ''
            """, (cutoff,))
            rows = cur.fetchall()
    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE order_item_info RESTART IDENTITY")
        if rows:
            execute_values(
                cur,
                "INSERT INTO order_item_info (order_id, sku, quantity) VALUES %s",
                rows,
            )
    pg.commit()
    log_step("order_item_info", len(rows), time.time() - t0)


def sync_ebay_products(pg):
    """
    Sync active listings only (is_deleted=0, is_ended=0).

    TWO separate fast queries instead of one slow COALESCE window function:
      Query 1: rows WITH mapped_sku    → PARTITION BY mapped_sku (fast, indexed)
      Query 2: rows WITHOUT mapped_sku → PARTITION BY sku        (fast, indexed)

    Both combined in Python — no COALESCE on large dataset.
    effective_sku stored in mapped_sku column so all downstream code works unchanged.
    """
    log.info("Syncing ebay_products (mapped_sku rows + NULL mapped_sku fallback) ...")
    t0 = time.time()
    with mysql_conn("listing_management") as my:
        with my.cursor() as cur:

            # ── Query 1: rows WITH mapped_sku (existing fast query) ────────────
            cur.execute("""
                SELECT TRIM(mapped_sku) AS effective_sku,
                       TRIM(sku)        AS orig_sku,
                       title, LOWER(which_channel) AS which_channel,
                       item_id, main_image_url, price, currency,
                       listing_url, status, product_description,
                       IFNULL(selected_variations, '') AS selected_variations
                FROM (
                    SELECT mapped_sku, sku, title, which_channel, item_id,
                           main_image_url, price, currency, listing_url, status,
                           product_description, selected_variations,
                           ROW_NUMBER() OVER (
                               PARTITION BY TRIM(mapped_sku)
                               ORDER BY
                                   (title IS NULL) ASC, (title = '') ASC,
                                   CASE LOWER(which_channel)
                                       WHEN 'amazon'  THEN 1
                                       WHEN 'ebay'    THEN 2
                                       WHEN 'shopify' THEN 3
                                       ELSE 4
                                   END ASC,
                                   LENGTH(title) DESC
                           ) AS rn
                    FROM ebay_products
                    WHERE is_deleted = 0
                      AND is_ended   = 0
                      AND mapped_sku IS NOT NULL
                      AND TRIM(mapped_sku) != ''
                ) ranked WHERE rn = 1
            """)
            rows_with_mapped = cur.fetchall()

            # ── Query 2: rows WITHOUT mapped_sku — use original sku ───────────
            # TRIM removed from WHERE — MySQL can use indexes on direct column comparison
            cur.execute("""
                SELECT TRIM(sku)  AS effective_sku,
                       TRIM(sku)  AS orig_sku,
                       title, LOWER(which_channel) AS which_channel,
                       item_id, main_image_url, price, currency,
                       listing_url, status, product_description,
                       IFNULL(selected_variations, '') AS selected_variations
                FROM (
                    SELECT sku, title, which_channel, item_id,
                           main_image_url, price, currency, listing_url, status,
                           product_description, selected_variations,
                           ROW_NUMBER() OVER (
                               PARTITION BY sku
                               ORDER BY
                                   (title IS NULL) ASC, (title = '') ASC,
                                   CASE LOWER(which_channel)
                                       WHEN 'amazon'  THEN 1
                                       WHEN 'ebay'    THEN 2
                                       WHEN 'shopify' THEN 3
                                       ELSE 4
                                   END ASC,
                                   LENGTH(title) DESC
                           ) AS rn
                    FROM ebay_products
                    WHERE is_deleted    = 0
                      AND is_ended      = 0
                      AND which_channel = 'amazon'
                      AND mapped_sku    IS NULL
                      AND sku           IS NOT NULL
                      AND sku          != ''
                ) ranked WHERE rn = 1
            """)
            rows_null_mapped = cur.fetchall()

    # Combine — mapped_sku rows take priority
    # effective_sku stored in mapped_sku column so all downstream code unchanged
    seen = set()
    pg_rows = []
    for row in rows_with_mapped + rows_null_mapped:
        effective_sku = row[0]
        if effective_sku and effective_sku not in seen:
            seen.add(effective_sku)
            # (mapped_sku=effective_sku, sku=orig_sku, title, channel, ...)
            pg_rows.append(row)

    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE ebay_products")
        if pg_rows:
            execute_values(
                cur,
                """INSERT INTO ebay_products
                   (mapped_sku, sku, title, which_channel, item_id,
                    main_image_url, price, currency,
                    listing_url, status, product_description,
                    selected_variations)
                   VALUES %s""",
                pg_rows,
            )
    pg.commit()
    log_step("ebay_products", len(pg_rows), time.time() - t0)


def sync_ebay_product_titles(pg):
    """
    Sync site-specific display data into ebay_product_titles table.
    One row per (mapped_sku, site) for UK / Germany / US.

    Stores: title, item_id (ASIN), listing_url — all site-specific.
    Why item_id per site: 3,250 SKUs have DIFFERENT ASINs per marketplace.
    Why listing_url per site: domain is site-specific
      UK      → amazon.co.uk
      Germany → amazon.de
      US      → amazon.com

    PARTITION BY TRIM(mapped_sku), site — picks best row per country:
      1. Title NOT NULL + NOT empty first
      2. Amazon before eBay before Shopify
      3. Longest title preferred

    LEFT JOINed with COALESCE in dashboard/remap/detail queries.
    Missing site row = graceful fallback to ebay_products global values.
    Does NOT affect product counts or filtering.
    """
    log.info("Syncing ebay_product_titles (site-specific: title + item_id + listing_url) ...")
    t0 = time.time()
    with mysql_conn("listing_management") as my:
        with my.cursor() as cur:
            # Query 1: rows WITH mapped_sku (fast — indexed on mapped_sku)
            cur.execute("""
                SELECT TRIM(mapped_sku) AS effective_sku, site, title, item_id, listing_url
                FROM (
                    SELECT mapped_sku, site, title, item_id, listing_url,
                           ROW_NUMBER() OVER (
                               PARTITION BY TRIM(mapped_sku), site
                               ORDER BY
                                   (title IS NULL) ASC, (title = '') ASC,
                                   CASE LOWER(which_channel)
                                       WHEN 'amazon' THEN 1 WHEN 'ebay' THEN 2
                                       WHEN 'shopify' THEN 3 ELSE 4
                                   END ASC, LENGTH(title) DESC
                           ) AS rn
                    FROM ebay_products
                    WHERE is_deleted  = 0 AND is_ended = 0
                      AND mapped_sku IS NOT NULL AND TRIM(mapped_sku) != ''
                      AND site IN ('UK', 'Germany', 'US')
                      AND title IS NOT NULL AND TRIM(title) != ''
                ) r WHERE rn = 1
            """)
            rows_with_mapped = cur.fetchall()

            # Query 2: rows WITHOUT mapped_sku — use original sku (Amazon only)
            # TRIM removed from WHERE — MySQL can use indexes on direct column comparison
            cur.execute("""
                SELECT TRIM(sku) AS effective_sku, site, title, item_id, listing_url
                FROM (
                    SELECT sku, site, title, item_id, listing_url,
                           ROW_NUMBER() OVER (
                               PARTITION BY sku, site
                               ORDER BY
                                   (title IS NULL) ASC, (title = '') ASC,
                                   CASE LOWER(which_channel)
                                       WHEN 'amazon' THEN 1 WHEN 'ebay' THEN 2
                                       WHEN 'shopify' THEN 3 ELSE 4
                                   END ASC, LENGTH(title) DESC
                           ) AS rn
                    FROM ebay_products
                    WHERE is_deleted    = 0
                      AND is_ended      = 0
                      AND which_channel = 'amazon'
                      AND mapped_sku    IS NULL
                      AND sku           IS NOT NULL
                      AND sku          != ''
                      AND site IN ('UK', 'Germany', 'US')
                      AND title IS NOT NULL
                      AND title        != ''
                ) r WHERE rn = 1
            """)
            rows_null_mapped = cur.fetchall()

            # Combine — mapped_sku rows take priority, deduplicate by (effective_sku, site)
            seen_keys = set()
            rows = []
            for row in rows_with_mapped + rows_null_mapped:
                key = (row[0], row[1])  # (effective_sku, site)
                if key not in seen_keys:
                    seen_keys.add(key)
                    rows.append(row)
    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE ebay_product_titles")
        if rows:
            execute_values(
                cur,
                """INSERT INTO ebay_product_titles
                   (mapped_sku, site, title, item_id, listing_url)
                   VALUES %s""",
                rows,
            )
    pg.commit()
    log_step("ebay_product_titles", len(rows), time.time() - t0)


def sync_ph_mapping(pg):
    """
    FIXED: Sync PH holder mapping from MySQL ph_cate_products.
    Replaces the unreliable Excel-file-based mapping.
    """
    log.info("Syncing ph_mapping from MySQL ph_cate_products ...")
    t0 = time.time()

    with mysql_conn("order_management") as my:
        with my.cursor() as cur:
            cur.execute("""
                SELECT
                    COALESCE(NULLIF(TRIM(ep.mapped_sku),''), TRIM(ep.sku)) AS effective_sku,
                    u.user_firstname AS holder_name
                FROM ph_cate_products pcp
                JOIN ph_categories pc ON pc.id    = pcp.ass_cate_id
                JOIN user          u  ON u.`user` = pc.user_id
                JOIN listing_management.ebay_products ep
                     ON ep.item_id = pcp.ref_id
                WHERE ep.sku IS NOT NULL
                  AND TRIM(ep.sku) != ''
                  AND pcp.which_channel = 1
            """)
            rows = cur.fetchall()

    # Normalize holder names
    normalized = []
    seen = set()
    for row in rows:
        mapped_sku  = row[0].strip()  # effective_sku (mapped_sku OR original sku)
        raw_name    = row[1].strip().title() if row[1] else "UNASSIGNED"

        name_map = {
            "Tharsika(Jaffna)":   "Tharsika",
            "Tharsika":           "Tharsika",
            "Tharsiga(Nell)":     "Tharshika - Nelliady",
            "Tharshika - Nelliady": "Tharshika - Nelliady",
            "Thuwaraga":          "Thuwaraha",
            "Illakkiya":          "Ilakkiya",
        }
        holder_name = name_map.get(raw_name, raw_name)

        if mapped_sku not in seen:
            seen.add(mapped_sku)
            normalized.append((mapped_sku, holder_name))

    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE ph_mapping")
        if normalized:
            execute_values(
                cur,
                """INSERT INTO ph_mapping (mapped_sku, holder_name)
                   VALUES %s ON CONFLICT (mapped_sku) DO UPDATE
                   SET holder_name = EXCLUDED.holder_name, synced_at = NOW()""",
                normalized,
            )
    pg.commit()
    log_step("ph_mapping", len(normalized), time.time() - t0)


def sync_amazon_variants(pg):
    """
    Sync amazon_variants — one row per mapped_sku (deduplicated).

    FIX: Previously stored one row per ASIN, meaning one mapped_sku could have
    multiple rows (different ASINs, different colors under the same parent).
    This caused the remap query to multiply OOS rows per color variant.

    Now: one row per mapped_sku — picks the best row using:
      1. Non-null color first
      2. Non-null asin first
      3. Most common parent_sku for that mapped_sku
    """
    log.info("Syncing amazon_variants (one row per effective_sku, two fast queries) ...")
    t0 = time.time()
    import json as _json

    with mysql_conn("listing_management") as my:
        with my.cursor() as cur:
            # Query 1: rows WITH mapped_sku
            # Site priority in Python dedup: UK > US > Germany > Ireland > others
            # This prevents Ireland listings contaminating UK parent_sku groupings
            cur.execute("""
                SELECT
                    TRIM(mapped_sku)        AS mapped_sku,
                    TRIM(parent_sku)        AS parent_sku,
                    TRIM(item_id)           AS asin,
                    IFNULL(selected_variations, '') AS variations,
                    TRIM(site)              AS site
                FROM ebay_products
                WHERE which_channel = 'amazon'
                  AND is_deleted    = 0
                  AND is_ended      = 0
                  AND mapped_sku   IS NOT NULL
                  AND TRIM(mapped_sku) != ''
                  AND parent_sku   IS NOT NULL
                  AND TRIM(parent_sku) != ''
            """)
            rows_with = cur.fetchall()

            # Query 2: rows WITHOUT mapped_sku — use original sku
            cur.execute("""
                SELECT
                    TRIM(sku)               AS mapped_sku,
                    TRIM(parent_sku)        AS parent_sku,
                    TRIM(item_id)           AS asin,
                    IFNULL(selected_variations, '') AS variations,
                    TRIM(site)              AS site
                FROM ebay_products
                WHERE which_channel = 'amazon'
                  AND is_deleted    = 0
                  AND is_ended      = 0
                  AND (mapped_sku IS NULL OR TRIM(mapped_sku) = '')
                  AND sku          IS NOT NULL
                  AND TRIM(sku)   != ''
                  AND parent_sku   IS NOT NULL
                  AND TRIM(parent_sku) != ''
            """)
            rows_null = cur.fetchall()

    rows = list(rows_with) + list(rows_null)

    # Site priority — UK rows should define parent_sku, not Ireland/other sites
    SITE_PRIORITY = {'UK': 0, 'US': 1, 'Germany': 2, 'Ireland': 9}

    def site_rank(site):
        return SITE_PRIORITY.get(site or '', 5)

    import re as _re
    def is_multipack(sku):
        """Detect multi-pack SKUs: 2PK, 3PK, 5PK etc. in any segment."""
        return bool(_re.search(r'\d+PK', sku.upper())) if sku else False

    # Build: for each (parent_sku, parts_count) → how many UK amazon SINGLE-UNIT members
    # Exclude multi-pack SKUs (2PK, 3PK, 5PK) from count
    # This prevents bundle families (QF-QQUZ-AKDK with 2PK+3PK) from winning
    # over the correct single-unit family (PH-8N0H-LTM8)
    from collections import defaultdict
    family_parts_count = defaultdict(int)  # (parent_sku, parts) → count
    for row in rows:
        mapped_sku_r, parent_sku_r, _, _, site_r = row
        if parent_sku_r and site_rank(site_r) == 0:  # UK only
            sku_key = mapped_sku_r or ''
            if is_multipack(sku_key):
                continue  # skip 2PK, 3PK, 5PK variants
            parts = len(sku_key.split('+')) if sku_key else 1
            family_parts_count[(parent_sku_r, parts)] += 1

    # Deduplicate — keep one best row per mapped_sku
    # Priority:
    #   1. Site rank (UK=0 beats Ireland=9)
    #   2. Most same-parts siblings in this family (parts match)
    #   3. Has color (tiebreaker)
    best = {}  # mapped_sku -> (entry, score)
    for row in rows:
        mapped_sku, parent_sku, asin, variations, site = row
        if not mapped_sku or not parent_sku:
            continue

        mapped_sku = mapped_sku.strip().lstrip('"\n').strip()
        if not mapped_sku:
            continue

        # Extract color from JSON
        color = ""
        try:
            if variations:
                var_list = _json.loads(variations)
                for v in var_list:
                    if isinstance(v, dict) and v.get("name", "").lower() == "color":
                        color = str(v.get("value", "")).strip()
                        break
        except Exception:
            color = ""

        entry    = (mapped_sku, parent_sku.strip(), asin or "", color)
        rank     = site_rank(site)
        parts    = len(mapped_sku.split('+'))

        # How many same-parts siblings does this family have? (negate for sort)
        same_parts_siblings = family_parts_count.get((parent_sku.strip(), parts), 0)
        has_color = 0 if color else 1  # 0=has color (better), 1=no color

        # Lower score = better
        # Priority 1: site rank
        # Priority 2: most same-parts siblings (negate so more = lower = better)
        # Priority 3: has color
        score = (rank, -same_parts_siblings, has_color)

        if mapped_sku not in best:
            best[mapped_sku] = (entry, score)
        else:
            _, existing_score = best[mapped_sku]
            if score < existing_score:
                best[mapped_sku] = (entry, score)

    cleaned = [v[0] for v in best.values()]

    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE amazon_variants")
        if cleaned:
            execute_values(
                cur,
                "INSERT INTO amazon_variants (mapped_sku, parent_sku, asin, color) VALUES %s",
                cleaned,
            )
    pg.commit()
    log_step("amazon_variants", len(cleaned), time.time() - t0)



def sync_bullet_points(pg):
    """
    Sync bullet points for all products.
    Join: listing_management.ebay_products ep
          JOIN bullet_points bp ON bp.product_id = ep.id
    Deduplication: one set of bullet points per mapped_sku
    (takes points from the first matching ebay_products row).
    """
    log.info("Syncing product_bullet_points ...")
    t0 = time.time()
    with mysql_conn("listing_management") as my:
        with my.cursor() as cur:
            cur.execute("""
                SELECT
                    TRIM(ep.mapped_sku) AS mapped_sku,
                    bp.points           AS point_text,
                    bp.view_order
                FROM ebay_products ep
                JOIN bullet_points bp ON bp.product_id = ep.id
                WHERE ep.is_deleted = 0
                  AND ep.mapped_sku IS NOT NULL
                  AND TRIM(ep.mapped_sku) != ''
                  AND bp.points IS NOT NULL
                  AND bp.points != ''
                ORDER BY TRIM(ep.mapped_sku), bp.view_order
            """)
            rows = cur.fetchall()

    # Deduplicate — keep only first occurrence per (mapped_sku, view_order)
    seen = set()
    cleaned = []
    for row in rows:
        mapped_sku, point_text, view_order = row
        if not mapped_sku:
            continue
        key = (mapped_sku, view_order)
        if key not in seen:
            seen.add(key)
            cleaned.append((mapped_sku, point_text, view_order))

    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE product_bullet_points")
        if cleaned:
            execute_values(
                cur,
                "INSERT INTO product_bullet_points (mapped_sku, point_text, view_order) VALUES %s",
                cleaned,
            )
    pg.commit()
    log_step("product_bullet_points", len(cleaned), time.time() - t0)


def sync_sub_images(pg):
    """
    Sync sub images for all products.
    Join: listing_management.ebay_products ep
          JOIN sub_images si ON si.product_id = ep.id
    Deduplication: unique image_url per mapped_sku.
    """
    log.info("Syncing product_sub_images ...")
    t0 = time.time()
    with mysql_conn("listing_management") as my:
        with my.cursor() as cur:
            cur.execute("""
                SELECT
                    TRIM(ep.mapped_sku) AS mapped_sku,
                    si.image_url,
                    si.view_order
                FROM ebay_products ep
                JOIN sub_images si ON si.product_id = ep.id
                WHERE ep.is_deleted = 0
                  AND ep.mapped_sku IS NOT NULL
                  AND TRIM(ep.mapped_sku) != ''
                  AND si.image_url IS NOT NULL
                  AND si.image_url != ''
                ORDER BY TRIM(ep.mapped_sku), si.view_order
            """)
            rows = cur.fetchall()

    # Deduplicate — unique image_url per mapped_sku
    seen = set()
    cleaned = []
    for row in rows:
        mapped_sku, image_url, view_order = row
        if not mapped_sku or not image_url:
            continue
        key = (mapped_sku, image_url)
        if key not in seen:
            seen.add(key)
            cleaned.append((mapped_sku, image_url, view_order))

    with pg.cursor() as cur:
        cur.execute("TRUNCATE TABLE product_sub_images")
        if cleaned:
            execute_values(
                cur,
                "INSERT INTO product_sub_images (mapped_sku, image_url, view_order) VALUES %s",
                cleaned,
            )
    pg.commit()
    log_step("product_sub_images", len(cleaned), time.time() - t0)


def sync_dashboard_cache(pg):
    """
    Pre-compute dashboard data for all locations and write to dashboard_cache table.

    Why: The live query (5-CTE PostgreSQL query) was running on every API request,
    taking 2-3 seconds per call and recomputing the same data repeatedly.
    Since data only changes every 20 minutes (sync cycle), it makes no sense to
    recompute on every browser click.

    This function runs AFTER all sync functions complete. It computes:
      - velocity (sold_7d / sold_14d / sold_30d) per SKU
      - avg_per_day using the same waterfall as compute_velocity()
      - status using the same logic as compute_status()
      - product_name with UK→US→Germany title priority
      - One row per (sku, location) for UK / US / Germany
      - One row per sku with location='ALL' (stock = sum of all locations)

    Result: /api/stock?marketplace=UK runs in ~200ms (simple SELECT)
    instead of ~2-3s (complex CTE + Python computation per request).

    Thresholds (aligned with dashboard_server.py):
      CRIT = 7   days
      LOW  = 21  days
      OVER = 90  days
    """
    log.info("Building dashboard_cache (pre-computed status for all locations) ...")
    t0 = time.time()

    CRIT = 7
    LOW  = 21
    OVER = 90

    with pg.cursor() as cur:
        # ── Fetch all base data from PostgreSQL ───────────────────────────────
        # Stock per sku per location
        cur.execute("""
            SELECT TRIM(sku) AS sku, location, SUM(stock) AS total_stock
            FROM location_wise_inv_stock
            GROUP BY TRIM(sku), location
        """)
        stock_rows = cur.fetchall()

        # Velocity (last 30 days)
        cur.execute("""
            SELECT
                TRIM(oii.sku) AS sku,
                COALESCE(SUM(CASE WHEN o.order_date >= CURRENT_DATE - INTERVAL '7 days'
                                  THEN oii.quantity END), 0) AS sold_7d,
                COALESCE(SUM(CASE WHEN o.order_date >= CURRENT_DATE - INTERVAL '14 days'
                                  THEN oii.quantity END), 0) AS sold_14d,
                COALESCE(SUM(oii.quantity), 0)                AS sold_30d
            FROM order_item_info oii
            JOIN orders o ON o.internal_id = oii.order_id
            GROUP BY TRIM(oii.sku)
        """)
        velocity_rows = cur.fetchall()

        # Product info — global fallback (platform, item_id) + per-site titles
        # Global product query for platform/item_id only
        cur.execute("""
            SELECT DISTINCT ON (TRIM(ep.mapped_sku))
                   TRIM(ep.mapped_sku)  AS sku,
                   ep.which_channel    AS platform,
                   ep.item_id,
                   ep.title            AS global_title
            FROM ebay_products ep
            WHERE ep.mapped_sku IS NOT NULL AND TRIM(ep.mapped_sku) != ''
            ORDER BY TRIM(ep.mapped_sku), (ep.title IS NULL) ASC, LENGTH(ep.title) DESC
        """)
        product_rows = cur.fetchall()

        # Per-site titles from ebay_product_titles
        # Returns one best title per (mapped_sku, site) for UK / Germany / US
        cur.execute("""
            SELECT mapped_sku, site, title
            FROM ebay_product_titles
            WHERE site IN ('UK', 'Germany', 'US')
              AND title IS NOT NULL
              AND TRIM(title) != ''
        """)
        site_title_rows = cur.fetchall()

        # Holders
        cur.execute("""
            SELECT TRIM(mapped_sku) AS sku, holder_name
            FROM ph_mapping
            WHERE holder_name != 'UNASSIGNED'
        """)
        holder_rows = cur.fetchall()

        # Extras (inbound, reorder_point)
        cur.execute("SELECT TRIM(sku) AS sku, inbound_units, reorder_point FROM sku_extras")
        extras_rows = cur.fetchall()

    # ── Build lookup dicts ────────────────────────────────────────────────────
    # velocity: sku → (sold_7d, sold_14d, sold_30d)
    velocity = {}
    for row in velocity_rows:
        velocity[row[0]] = (int(row[1] or 0), int(row[2] or 0), int(row[3] or 0))

    # products: sku → (platform, item_id, global_title)
    products = {}
    for row in product_rows:
        products[row[0]] = (row[1] or "", row[2] or "", row[3] or "")

    # site_titles: (sku, site) → title
    # Used to pick the correct language title per location row
    site_titles = {}
    for row in site_title_rows:
        site_titles[(row[0], row[1])] = row[2] or ""

    def get_title(sku, location):
        """
        Title priority per location (mirrors dashboard_server.py COALESCE chain):
          UK      → UK title → US title → Germany title → global
          Germany → Germany title → UK title → US title → global
          US      → US title → UK title → Germany title → global
          ALL     → UK title → US title → Germany title → global
        """
        global_title = products.get(sku, ("", "", ""))[2]
        uk  = site_titles.get((sku, "UK"),      "")
        us  = site_titles.get((sku, "US"),      "")
        de  = site_titles.get((sku, "Germany"), "")
        if location == "UK":
            title = uk or us or de or global_title
        elif location == "Germany":
            title = de or uk or us or global_title
        elif location == "US":
            title = us or uk or de or global_title
        else:  # ALL
            title = uk or us or de or global_title
        title = title.strip()
        return (title[:60] + "…") if len(title) > 60 else title

    # holders: sku → holder_name
    holders = {row[0]: row[1] for row in holder_rows}

    # extras: sku → (inbound, reorder_point)
    extras = {row[0]: (int(row[1] or 0), int(row[2] or 0)) for row in extras_rows}

    # stock_by_loc: (sku, location) → stock
    stock_by_loc = {}
    # stock_all: sku → total stock across all locations
    stock_all = {}
    for row in stock_rows:
        sku, location, stock = row[0], row[1], int(row[2] or 0)
        stock_by_loc[(sku, location)] = stock
        stock_all[sku] = stock_all.get(sku, 0) + stock

    # ── Velocity + status helper (mirrors dashboard_server.py exactly) ────────
    def compute_avg(s7, s14, s30):
        if s7  >= 7: return round(s7  / 7,  2)
        if s14 >= 7: return round(s14 / 14, 2)
        if s30 >= 7: return round(s30 / 30, 2)
        return 0.0

    def compute_status(stock, avg):
        if avg == 0:
            return "NO DATA", None
        days = max(0, int(stock / avg))
        if days <= CRIT: return "CRITICAL",    days
        if days <= LOW:  return "LOW",         days
        if days <= OVER: return "HEALTHY",     days
        return "OVERSTOCKED", days

    # ── Build cache rows ──────────────────────────────────────────────────────
    # IMPORTANT: Use stock_by_loc as the driving set — exactly matching the old
    # query which had stock_agg as the driving table.
    # Only SKUs that appear in location_wise_inv_stock are included.
    # SKUs with holder + product but NO stock row are excluded (same as before).
    # This keeps the count consistent with the original 3606 figure.

    # All SKUs that have at least one stock row (any location)
    skus_with_stock = set(sku for (sku, loc) in stock_by_loc.keys())

    # Must also have holder + product info (same JOIN conditions as old query)
    all_skus = skus_with_stock & set(holders.keys()) & set(products.keys())

    cache_rows = []
    locations = ["UK", "US", "Germany"]

    for sku in all_skus:
        s7, s14, s30 = velocity.get(sku, (0, 0, 0))
        avg = compute_avg(s7, s14, s30)
        platform, item_id, _ = products[sku]   # global_title used via get_title()
        holder = holders[sku]
        inbound, reorder = extras.get(sku, (0, 0))

        # Per-location rows — each gets its own language-correct title
        for loc in locations:
            stock = stock_by_loc.get((sku, loc), 0)
            status, days = compute_status(stock, avg)
            prod_name = get_title(sku, loc)     # UK→German for Germany, English for UK/US
            sort_order = {'CRITICAL':0,'LOW':1,'HEALTHY':2,'OVERSTOCKED':3}.get(status, 4)
            cache_rows.append((
                sku, loc, prod_name, platform, item_id,
                stock, inbound, reorder,
                s7, s14, s30,
                avg, days, status, holder, sort_order
            ))

        # ALL row — sum of all locations, UK title priority (best English title)
        total_stock = stock_all.get(sku, 0)
        status_all, days_all = compute_status(total_stock, avg)
        prod_name_all = get_title(sku, "ALL")
        sort_all = {'CRITICAL':0,'LOW':1,'HEALTHY':2,'OVERSTOCKED':3}.get(status_all, 4)
        cache_rows.append((
            sku, "ALL", prod_name_all, platform, item_id,
            total_stock, inbound, reorder,
            s7, s14, s30,
            avg, days_all, status_all, holder, sort_all
        ))

    # ── Zero-downtime write — never leaves dashboard_cache empty ─────────────
    # Problem: TRUNCATE then INSERT leaves table empty for ~15 seconds.
    #          Any Flask /api/stock request during that window returns 0 rows.
    #          This happens every 20-minute sync cycle.
    #
    # Solution: Write to a staging table first, then swap atomically.
    #   Step 1: TRUNCATE dashboard_cache_staging (hidden from Flask)
    #   Step 2: INSERT all rows into staging (takes ~15s — Flask reads live table)
    #   Step 3: In ONE transaction: TRUNCATE live + INSERT from staging
    #           This transaction commits in milliseconds — Flask sees no gap.
    #
    # Flask always reads from dashboard_cache (live table).
    # dashboard_cache_staging is invisible to Flask endpoints.
    with pg.cursor() as cur:
        # Ensure staging table exists with same schema
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dashboard_cache_staging
            (LIKE dashboard_cache INCLUDING ALL)
        """)
        # Step 1+2: Populate staging table (safe — Flask does not read this)
        cur.execute("TRUNCATE TABLE dashboard_cache_staging")
        if cache_rows:
            execute_values(cur, """
                INSERT INTO dashboard_cache_staging
                (sku, location, product_name, platform, item_id,
                 stock, inbound, reorder_point,
                 sold_7d, sold_14d, sold_30d,
                 avg_per_day, days_remaining, status, holder, sort_order)
                VALUES %s
            """, cache_rows)
        pg.commit()  # staging data committed

        # Step 3: Atomic swap — TRUNCATE live + INSERT from staging in one transaction
        # This transaction takes milliseconds (in-PostgreSQL data copy)
        # Flask reads old data until this commit, then instantly sees new data
        cur.execute("TRUNCATE TABLE dashboard_cache")
        cur.execute("""
            INSERT INTO dashboard_cache
            SELECT * FROM dashboard_cache_staging
        """)
    pg.commit()  # ← single commit — old→new in one atomic step, zero empty window
    log_step("dashboard_cache", len(cache_rows), time.time() - t0)
    log.info(f"  → {len(all_skus):,} SKUs × 4 locations = {len(cache_rows):,} cache rows")


def main():
    start = time.time()
    log.info("=" * 60)
    log.info(f"db_sync starting at {datetime.now().isoformat()}")
    try:
        pg = pg_conn()
    except Exception as e:
        log.error(f"Cannot connect to PostgreSQL: {e}")
        sys.exit(1)
    try:
        ensure_tables(pg)
        sync_location_wise_inv_stock(pg)   # FIXED: was sync_inv_final_stock
        sync_orders(pg)
        sync_order_item_info(pg)           # FIXED: correct column names
        sync_ebay_products(pg)             # ONE row per mapped_sku (site-independent)
        sync_ebay_product_titles(pg)       # site-specific titles for display only
        sync_ph_mapping(pg)                # FIXED: from MySQL, not Excel
        sync_amazon_variants(pg)           # for remap suggestions page
        sync_dashboard_cache(pg)           # PRE-COMPUTE: done here — dashboard fresh in ~66s
        # ↑ Dashboard and Remap pages are fully up to date from this point.
        # The next two syncs are only for product_detail_card page.
        # They take 90+ seconds but do NOT block dashboard freshness.
        sync_bullet_points(pg)             # product detail only — 50,708 rows ~55s
        sync_sub_images(pg)                # product detail only — 96,064 rows ~35s
        elapsed = time.time() - start
        log.info(f"db_sync SUCCESS — total {elapsed:.1f}s")
    except Exception as e:
        log.error(f"db_sync FAILED: {e}", exc_info=True)
        sys.exit(1)
    finally:
        pg.close()

if __name__ == "__main__":
    main()