#!/usr/bin/env python3
"""
LEDs One — product_detail_card.py
Full product detail page — landscape view, same-page navigation.

Accessed via: http://192.168.18.94:8080/product-detail-card?sku=ENC5693&location=UK
              http://192.168.18.94:8080/product-detail-card?sku=ENC5693&compare=CRSF1202BM&location=UK

Called by:
  - Remap page Details button (replaces modal — opens full page, same tab)
  - Dashboard server Remap column button (same tab navigation)

All data fetched from PostgreSQL stock_level db (synced every 20 min by db_sync.py).
ebay_products now contains: title, main_image_url, price, currency, listing_url,
status, product_description, selected_variations.

Returns full HTML page — not a JSON API.
"""

import os
import re
import json as _json
from flask import request, render_template_string
import psycopg2
import psycopg2.extras
import yaml

CONFIG_PATH = os.environ.get(
    "DASHBOARD_CONFIG",
    "/opt/openclaw/stock_level/config/stock_dashboard.yaml"
)

def _pg_conn():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        pg = cfg.get("postgres", {})
    else:
        pg = {}
    args = {
        "host":   pg.get("host",     "localhost"),
        "port":   pg.get("port",     5432),
        "dbname": pg.get("dbname",   "stock_level"),
        "user":   pg.get("user",     "digit_web"),
    }
    if pg.get("password"):
        args["password"] = pg["password"]
    return psycopg2.connect(**args)


def _fetch_product(sku, location):
    """
    Fetches product detail + stock for a given SKU and location from PostgreSQL.
    Returns a dict with all fields needed for the detail card.
    """
    if not sku:
        return None
    try:
        conn = _pg_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Product detail from ebay_products
            # Title, ASIN (item_id), listing_url priority: selected location → UK → US → global
            # NULLIF(...,'') treats empty string same as NULL so fallback chain works
            # item_id (ASIN): 3,250 SKUs have different ASINs per marketplace
            # listing_url: domain is site-specific (amazon.co.uk / .de / .com)
            cur.execute("""
                SELECT
                    ep.mapped_sku,
                    COALESCE(
                        NULLIF(TRIM(ept_loc.title), ''),
                        NULLIF(TRIM(ept_uk.title),  ''),
                        NULLIF(TRIM(ept_us.title),  ''),
                        ep.title
                    )                       AS title,
                    ep.which_channel,
                    COALESCE(
                        NULLIF(TRIM(ept_loc.item_id), ''),
                        NULLIF(TRIM(ept_uk.item_id),  ''),
                        NULLIF(TRIM(ept_us.item_id),  ''),
                        ep.item_id
                    )                       AS item_id,
                    ep.main_image_url,
                    ep.price,
                    ep.currency,
                    COALESCE(
                        NULLIF(TRIM(ept_loc.listing_url), ''),
                        NULLIF(TRIM(ept_uk.listing_url),  ''),
                        NULLIF(TRIM(ept_us.listing_url),  ''),
                        ep.listing_url
                    )                       AS listing_url,
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
            ep = cur.fetchone()

            # Stock for selected location
            cur.execute("""
                SELECT SUM(stock) AS total_stock
                FROM location_wise_inv_stock
                WHERE TRIM(sku) = %s
                  AND location  = %s
            """, (sku, location))
            st = cur.fetchone()

            # Stock for ALL locations
            cur.execute("""
                SELECT location, SUM(stock) AS stock
                FROM location_wise_inv_stock
                WHERE TRIM(sku) = %s
                GROUP BY location
                ORDER BY location
            """, (sku,))
            all_locs = cur.fetchall()

            # Holder
            cur.execute("""
                SELECT holder_name
                FROM ph_mapping
                WHERE mapped_sku = %s
                LIMIT 1
            """, (sku,))
            ph = cur.fetchone()

            # Sales velocity (30 days)
            cur.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN o.order_date >= CURRENT_DATE - INTERVAL '7 days'
                                     THEN oii.quantity END), 0) AS sold_7d,
                    COALESCE(SUM(CASE WHEN o.order_date >= CURRENT_DATE - INTERVAL '30 days'
                                     THEN oii.quantity END), 0) AS sold_30d
                FROM order_item_info oii
                JOIN orders o ON o.internal_id = oii.order_id
                WHERE TRIM(oii.sku) = %s
            """, (sku,))
            vel = cur.fetchone()

            # Bullet points ordered by view_order
            cur.execute("""
                SELECT point_text
                FROM product_bullet_points
                WHERE mapped_sku = %s
                ORDER BY view_order
            """, (sku,))
            bullets = [r["point_text"] for r in cur.fetchall()]

            # Sub images ordered by view_order (deduplicated)
            cur.execute("""
                SELECT DISTINCT image_url
                FROM product_sub_images
                WHERE mapped_sku = %s
                ORDER BY image_url
            """, (sku,))
            sub_imgs = [r["image_url"] for r in cur.fetchall()]

        conn.close()
    except Exception as e:
        return {"error": str(e)}

    if not ep:
        return None

    # Strip HTML from description
    desc_raw = ep.get("product_description") or ""
    desc_clean = re.sub(r"<[^>]+>", " ", desc_raw)
    desc_clean = re.sub(r"[ \t]+", " ", desc_clean)
    desc_clean = re.sub(r"\n{3,}", "\n\n", desc_clean).strip()

    # Parse variations
    variations = []
    try:
        sv = ep.get("selected_variations") or "[]"
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

    return {
        "sku":          ep.get("mapped_sku") or sku,
        "title":        ep.get("title") or sku,
        "channel":      ep.get("which_channel") or "",
        "asin":         ep.get("item_id") or "",
        "image_url":    ep.get("main_image_url") or "",
        "price":        float(ep.get("price") or 0),
        "currency":     ep.get("currency") or "GBP",
        "listing_url":  ep.get("listing_url") or "",
        "status":       ep.get("status") or "",
        "description":  desc_clean,
        "variations":   variations,
        "stock":        int((st or {}).get("total_stock") or 0),
        "stock_by_loc": [{"location": r["location"], "stock": int(r["stock"] or 0)}
                         for r in (all_locs or [])],
        "holder":       (ph or {}).get("holder_name") or "",
        "sold_7d":      int((vel or {}).get("sold_7d") or 0),
        "sold_30d":     int((vel or {}).get("sold_30d") or 0),
        "bullet_points": bullets,
        "sub_images":   sub_imgs,
    }


def get_detail_card_html():
    """Served at /product-detail-card"""
    sku         = request.args.get("sku", "").strip()
    compare_sku = request.args.get("compare", "").strip()
    location    = request.args.get("location", "UK").strip()
    back        = request.args.get("back", "/remap")

    if location not in ("UK", "US", "Germany"):
        location = "UK"

    cur_product  = _fetch_product(sku, location) if sku else None
    sug_product  = _fetch_product(compare_sku, location) if compare_sku else None

    error = None
    if sku and cur_product is None:
        error = f"No product found for SKU: {sku}"
    elif cur_product and "error" in cur_product:
        error = cur_product["error"]

    # Pass data as JSON strings into the template
    import json
    cur_json = json.dumps(cur_product or {})
    sug_json = json.dumps(sug_product or {})

    return render_template_string(
        DETAIL_CARD_HTML,
        sku=sku,
        compare_sku=compare_sku,
        location=location,
        back=back,
        error=error,
        cur_json=cur_json,
        sug_json=sug_json,
        has_compare=bool(sug_product),
    )


DETAIL_CARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Product Detail &mdash; {{ sku }}</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#070b12;--panel:#0d1219;--card:#111827;--card2:#162032;
  --line:#1a2535;--line2:#1f2d42;
  --text:#c9d5e0;--text2:#8fa3b8;--text3:#566a80;--white:#edf2f7;
  --red:#f87171;--red-d:#dc2626;--red-glow:rgba(220,38,38,.2);
  --amber:#fbbf24;--amber-d:#b45309;
  --emerald:#34d399;--emerald-d:#059669;--emerald-glow:rgba(5,150,105,.2);
  --sky:#38bdf8;--sky-d:#0284c7;
  --purple:#a78bfa;
}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;overflow:hidden;}
body{background:var(--bg);color:var(--text);font-family:"Inter",sans-serif;
  font-size:13px;display:flex;flex-direction:column;}

/* ══ HERO HEADER ══════════════════════════════════════════════════════ */
.hero{background:linear-gradient(135deg,#0d1219 0%,#0f1f35 50%,#0d1219 100%);
  border-bottom:1px solid var(--line2);flex-shrink:0;padding:0 24px;}
.hero-inner{max-width:100%;display:flex;align-items:center;gap:14px;height:58px;}
.hero-back{display:inline-flex;align-items:center;gap:5px;color:var(--sky);
  text-decoration:none;font-size:0.72rem;font-weight:600;
  background:rgba(56,189,248,.07);border:1px solid rgba(56,189,248,.18);
  border-radius:6px;padding:5px 11px;transition:all .15s;flex-shrink:0;}
.hero-back:hover{background:rgba(56,189,248,.15);}
.hero-divider{width:1px;height:22px;background:var(--line2);}
.hero-title{font-size:0.8rem;font-weight:700;color:var(--white);
  display:flex;align-items:center;gap:8px;}
.hero-badge{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;
  border-radius:5px;font-size:0.6rem;font-weight:700;letter-spacing:.07em;
  text-transform:uppercase;}
.hero-badge.hb-remap{background:rgba(220,38,38,.12);border:1px solid rgba(220,38,38,.28);color:var(--red);}
.hero-sku{font-family:"JetBrains Mono",monospace;font-size:0.75rem;font-weight:600;
  color:var(--white);background:rgba(255,255,255,.06);border:1px solid var(--line2);
  border-radius:5px;padding:3px 9px;flex-shrink:0;}
.hero-arrow{color:var(--text3);font-size:0.95rem;flex-shrink:0;}
.hero-sug-sku{font-family:"JetBrains Mono",monospace;font-size:0.75rem;font-weight:600;
  color:var(--emerald);background:rgba(5,150,105,.08);border:1px solid rgba(52,211,153,.22);
  border-radius:5px;padding:3px 9px;flex-shrink:0;}
.hero-loc{font-size:0.65rem;color:var(--sky);background:rgba(56,189,248,.07);
  border:1px solid rgba(56,189,248,.18);border-radius:4px;padding:2px 8px;flex-shrink:0;}
.hero-sp{flex:1;}
.hero-ts{font-size:0.6rem;color:var(--text3);font-family:"JetBrains Mono",monospace;}

/* ══ MAIN AREA — fills remaining height ═══════════════════════════════ */
.main-area{flex:1;overflow:hidden;display:flex;flex-direction:column;}

/* ══ COMPARE STRIP — fills width with 16px side padding ══════════════ */
.compare-strip{flex:1;display:grid;gap:15px;overflow:hidden;min-height:0;padding:10px 30px;}
.compare-strip.two-col{grid-template-columns:1fr 1fr;}
.compare-strip.one-col{grid-template-columns:1fr;}

/* ══ PRODUCT PANEL ════════════════════════════════════════════════════ */
.prod-panel{display:flex;flex-direction:column;overflow:hidden;min-height:0;
  border:1px solid var(--line2);border-radius:12px;background:var(--panel);}
.prod-panel.pp-cur{border-top:3px solid var(--red-d);box-shadow:0 0 0 0 transparent,0 4px 24px rgba(0,0,0,.4);}
.prod-panel.pp-sug{border-top:3px solid var(--emerald-d);box-shadow:0 4px 24px rgba(0,0,0,.4);}

/* Panel header */
.pp-hdr{display:flex;align-items:center;gap:10px;padding:10px 16px;
  border-bottom:1px solid var(--line);flex-shrink:0;}
.pp-hdr-lbl{font-size:0.58rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  padding:2px 8px;border-radius:4px;}
.pp-cur .pp-hdr-lbl{background:rgba(220,38,38,.12);color:var(--red);}
.pp-sug .pp-hdr-lbl{background:rgba(5,150,105,.1);color:var(--emerald);}
.pp-hdr-sku{font-family:"JetBrains Mono",monospace;font-size:0.72rem;font-weight:600;color:var(--white);}
.pp-hdr-sp{flex:1;}
.pp-hdr-status{font-size:0.62rem;color:var(--text3);font-weight:500;}
.pp-hdr-status.discoverable{color:var(--emerald);}
.pp-hdr-status.inactive{color:var(--red);}

/* Panel body — scrollable */
.pp-body{flex:1;overflow-y:auto;min-height:0;display:grid;
  grid-template-columns:200px 1fr;gap:0;}
.pp-body::-webkit-scrollbar{width:4px;}
.pp-body::-webkit-scrollbar-thumb{background:var(--line2);border-radius:2px;}

/* Left col — images — fixed 220px, consistent across both panels */
.pp-img-col{background:rgba(0,0,0,.2);border-right:1px solid var(--line);
  padding:14px 10px;display:flex;flex-direction:column;align-items:center;gap:10px;
  position:sticky;top:0;align-self:start;}
.pp-main-img{width:170px;height:170px;object-fit:contain;border-radius:8px;
  background:rgba(255,255,255,.03);border:1px solid var(--line2);padding:4px;
  cursor:pointer;transition:border-color .15s;}
.pp-main-img:hover{border-color:var(--sky);}
.pp-img-placeholder{width:170px;height:170px;border-radius:8px;
  background:var(--card2);border:1px solid var(--line2);
  display:flex;align-items:center;justify-content:center;font-size:2.5rem;color:var(--text3);}
.pp-price{background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.25);
  color:var(--amber);border-radius:6px;padding:5px 10px;
  font-size:0.76rem;font-weight:700;font-family:"JetBrains Mono",monospace;
  text-align:center;width:100%;}
.pp-channel{font-size:0.62rem;color:var(--sky);text-transform:capitalize;text-align:center;}

/* Thumbnail gallery — fixed 48px thumbnails, consistent in both panels */
.gallery-wrap{width:100%;display:flex;flex-direction:column;gap:6px;}
.gallery-label{font-size:0.56rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:var(--text3);text-align:center;}
.gallery-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;}
.g-thumb{width:100%;aspect-ratio:1;object-fit:contain;border-radius:4px;
  background:rgba(255,255,255,.03);border:1px solid var(--line2);
  cursor:pointer;padding:2px;transition:border-color .12s;}
.g-thumb:hover{border-color:var(--sky);}
.g-thumb.active{border-color:var(--emerald);box-shadow:0 0 0 1px rgba(52,211,153,.4);}
.gallery-nav{display:flex;align-items:center;justify-content:space-between;gap:4px;width:100%;}
.gn-btn{background:var(--card2);border:1px solid var(--line2);color:var(--text2);
  border-radius:4px;padding:3px 8px;font-size:0.62rem;cursor:pointer;
  transition:all .12s;font-family:"Inter",sans-serif;}
.gn-btn:hover:not(:disabled){background:var(--line2);color:var(--white);}
.gn-btn:disabled{opacity:.3;cursor:default;}
.gn-info{font-size:0.6rem;color:var(--text3);font-family:"JetBrains Mono",monospace;text-align:center;}

/* Right col — details */
.pp-detail-col{padding:14px 16px;display:flex;flex-direction:column;gap:12px;}

.pp-title{font-size:0.85rem;font-weight:600;color:var(--white);line-height:1.4;}

/* Stock grid */
.stock-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;}
.stock-item{background:var(--card2);border:1px solid var(--line2);border-radius:7px;
  padding:20px 6px;text-align:center;transition:all .2s;}
.stock-item.loc-selected{
  border-color:var(--sky);
  box-shadow:0 0 0 2px rgba(56,189,248,.25),inset 0 0 0 1px rgba(56,189,248,.15);
  background:rgba(56,189,248,.06);
  position:relative;
}
.stock-item.loc-selected::after{
  content:'Selected';
  position:absolute;bottom:-8px;left:50%;transform:translateX(-50%);
  background:var(--sky);color:#000;font-size:0.48rem;font-weight:800;
  letter-spacing:.06em;text-transform:uppercase;border-radius:999px;
  padding:1px 6px;white-space:nowrap;
}
.stock-item.loc-selected .si-loc{color:var(--sky);}
.si-loc{font-size:0.56rem;color:var(--text3);text-transform:uppercase;
  letter-spacing:.07em;margin-bottom:3px;font-weight:600;}
.si-val{font-size:1.1rem;font-weight:800;font-family:"JetBrains Mono",monospace;}
.si-val.zero{color:var(--red);}
.si-val.low{color:var(--amber);}
.si-val.good{color:var(--emerald);}

/* Velocity */
.vel-row{display:grid;grid-template-columns:1fr 1fr;gap:6px;}
.vel-item{background:var(--card2);border:1px solid var(--line2);border-radius:6px;
  padding:7px 10px;text-align:center;}
.vel-val{font-size:1rem;font-weight:700;font-family:"JetBrains Mono",monospace;color:var(--sky);}
.vel-lbl{font-size:0.56rem;color:var(--text3);margin-top:1px;text-transform:uppercase;letter-spacing:.05em;}

/* Field */
.field{display:flex;flex-direction:column;gap:2px;}
.f-lbl{font-size:0.57rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text3);}
.f-val{font-size:0.78rem;color:var(--text);}
.f-val.mono{font-family:"JetBrains Mono",monospace;font-size:0.7rem;}
.f-val.na{color:var(--text3);font-style:italic;}
.alink{color:var(--sky);text-decoration:none;font-size:0.7rem;}
.alink:hover{text-decoration:underline;}

/* Variations */
.var-wrap{display:flex;flex-wrap:wrap;gap:4px;}
.var-tag{background:rgba(56,189,248,.07);border:1px solid rgba(56,189,248,.15);
  color:var(--sky);border-radius:5px;padding:2px 8px;font-size:0.64rem;}
.var-tag .vn{color:var(--text3);}

/* Bullet points */
.bullets-box{background:var(--card2);border:1px solid var(--line2);border-radius:7px;
  padding:10px 12px;}
.box-lbl{font-size:0.57rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;
  color:var(--text3);margin-bottom:7px;}
.bullet-list{padding-left:16px;display:flex;flex-direction:column;gap:5px;}
.bullet-list li{font-size:0.74rem;color:var(--text2);line-height:1.5;}

/* Description */
.desc-box{background:var(--card2);border:1px solid var(--line2);border-radius:7px;padding:10px 12px;}
.desc-text{font-size:0.73rem;color:var(--text2);line-height:1.6;
  white-space:pre-wrap;max-height:160px;overflow-y:auto;}
.desc-text::-webkit-scrollbar{width:3px;}
.desc-text::-webkit-scrollbar-thumb{background:var(--line2);}

/* ══ BOTTOM ACTION BAR ════════════════════════════════════════════════ */
.action-bar{background:var(--panel);border-top:1px solid var(--line2);
  padding:10px 20px;display:flex;align-items:center;gap:12px;flex-shrink:0;}
.ab-label{font-size:0.7rem;color:var(--text2);font-weight:600;}
.ab-sp{flex:1;}
.btn-amz{background:#ff9900;border:none;color:#000;border-radius:7px;
  padding:7px 16px;font-size:0.75rem;font-weight:700;cursor:pointer;
  font-family:"Inter",sans-serif;text-decoration:none;
  display:inline-flex;align-items:center;gap:5px;transition:opacity .15s;}
.btn-amz:hover{opacity:.88;}
.btn-amz.off{opacity:.3;pointer-events:none;}
.btn-sug{background:rgba(5,150,105,.12);border:1px solid rgba(52,211,153,.25);
  color:var(--emerald);border-radius:7px;padding:7px 16px;font-size:0.75rem;
  font-weight:700;cursor:pointer;font-family:"Inter",sans-serif;
  text-decoration:none;display:inline-flex;align-items:center;gap:5px;}
.btn-sug:hover{background:rgba(5,150,105,.22);}
.btn-sug.off{opacity:.3;pointer-events:none;}

/* ══ CHECKLIST DRAWER ═════════════════════════════════════════════════ */
.cl-wrap{background:var(--panel);border-top:1px solid var(--line2);
  overflow:hidden;transition:max-height .3s ease;flex-shrink:0;}
.cl-wrap.open{max-height:240px;}
.cl-wrap.closed{max-height:0;}
.cl-inner{padding:12px 20px 14px;}
.cl-title{font-size:0.6rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;
  color:var(--amber);margin-bottom:10px;display:flex;align-items:center;gap:6px;}
.cl-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px 20px;}
.ci{display:flex;align-items:flex-start;gap:7px;cursor:pointer;}
.ci input{margin-top:1px;accent-color:var(--sky);flex-shrink:0;}
.ci span{font-size:0.72rem;color:var(--text2);line-height:1.3;}

/* ══ CHECKLIST TOGGLE BTN ════════════════════════════════════════════ */
.cl-toggle{background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2);
  color:var(--amber);border-radius:6px;padding:5px 12px;
  font-size:0.7rem;font-weight:600;cursor:pointer;
  font-family:"Inter",sans-serif;transition:background .15s;
  display:inline-flex;align-items:center;gap:5px;}
.cl-toggle:hover{background:rgba(251,191,36,.14);}

/* ══ ERROR ════════════════════════════════════════════════════════════ */
.err-box{margin:24px;padding:20px;background:rgba(220,38,38,.07);
  border:1px solid rgba(220,38,38,.2);border-radius:10px;
  color:var(--red);text-align:center;}

@media(max-width:1000px){
  .compare-strip.two-col{grid-template-columns:1fr;}
  .cl-grid{grid-template-columns:1fr 1fr;}
  .pp-body{grid-template-columns:160px 1fr;}
}
</style>
</head>
<body>

<!-- ══ HERO HEADER ════════════════════════════════════════════════════ -->
<div class="hero">
  <div class="hero-inner">
    <a href="{{ back }}" class="hero-back">&#8592; Back</a>
    <div class="hero-divider"></div>
    <span class="hero-badge hb-remap">&#9888; Remap Review</span>
    {% if sku %}<span class="hero-sku">{{ sku }}</span>{% endif %}
    {% if has_compare %}
    <span class="hero-arrow">&#8594;</span>
    <span class="hero-sug-sku">{{ compare_sku }}</span>
    {% endif %}
    <span class="hero-loc">{{ location }}</span>
    <div class="hero-sp"></div>
    <span class="hero-ts" id="hero-ts">Loaded from PostgreSQL</span>
  </div>
</div>

<!-- ══ MAIN AREA ═══════════════════════════════════════════════════════ -->
<div class="main-area">

{% if error %}
  <div class="err-box">&#9888; {{ error }}</div>
{% else %}

  <!-- Compare strip fills full width + height -->
  <div class="compare-strip {% if has_compare %}two-col{% else %}one-col{% endif %}" id="compare-strip">
    <div class="prod-panel pp-cur" id="panel-cur"></div>
    {% if has_compare %}
    <div class="prod-panel pp-sug" id="panel-sug"></div>
    {% endif %}
  </div>

{% endif %}
</div>

<!-- ══ ACTION BAR ══════════════════════════════════════════════════════ -->
<div class="action-bar">
  <span class="ab-label">&#128279; Amazon Listing Actions</span>
  <button class="cl-toggle" onclick="toggleChecklist()" id="cl-btn">&#9997; Checklist</button>
  <div class="ab-sp"></div>
  <a id="btn-cur" href="#" target="_blank" class="btn-amz off">Current Listing</a>
  {% if has_compare %}
  <a id="btn-sug" href="#" target="_blank" class="btn-sug off">&#10003; Suggested Listing</a>
  {% endif %}
</div>

<!-- ══ CHECKLIST DRAWER ════════════════════════════════════════════════ -->
<div class="cl-wrap closed" id="cl-wrap">
  <div class="cl-inner">
    <div class="cl-title">&#9997; Complete before executing remap</div>
    <div class="cl-grid">
      <label class="ci"><input type="checkbox"><span>Verify same product design and specifications</span></label>
      <label class="ci"><input type="checkbox"><span>Confirm only color or variant differs</span></label>
      <label class="ci"><input type="checkbox"><span>Update Amazon listing SKU to replacement</span></label>
      <label class="ci"><input type="checkbox"><span>Replace product images for new color</span></label>
      <label class="ci"><input type="checkbox"><span>Update title if original color is mentioned</span></label>
      <label class="ci"><input type="checkbox"><span>Update bullet points if color is referenced</span></label>
      <label class="ci"><input type="checkbox"><span>Verify listing is active 24&ndash;48h after update</span></label>
      <label class="ci"><input type="checkbox"><span>Confirm stock reflects on Amazon Seller Central</span></label>
    </div>
  </div>
</div>

<script>
var CUR = {{ cur_json | safe }};
var SUG = {{ sug_json | safe }};
var LOC = "{{ location }}";
var PAGE_SIZE = 13; // max thumbnails per page

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

function stockClass(v){ return v===0?'zero':v<=10?'low':'good'; }

// ── Build a product panel ────────────────────────────────────────────
function buildPanel(data, panelId, labelText, isCur){
  var panel = document.getElementById(panelId);
  if(!panel || !data || !data.sku) return;

  var statusText = data.status || '';
  var statusCls  = statusText.toLowerCase()==='active'||statusText.toLowerCase()==='discoverable'
                   ? 'discoverable' : 'inactive';

  // Header
  panel.innerHTML =
    '<div class="pp-hdr">'
      +'<span class="pp-hdr-lbl">'+esc(labelText)+'</span>'
      +'<span class="pp-hdr-sku">'+esc(data.sku)+'</span>'
      +'<span class="pp-hdr-sp"></span>'
      +'<span class="pp-hdr-status '+statusCls+'">'+esc(statusText)+'</span>'
    +'</div>'
    +'<div class="pp-body">'
      +'<div class="pp-img-col" id="'+panelId+'-imgcol"></div>'
      +'<div class="pp-detail-col" id="'+panelId+'-det"></div>'
    +'</div>';

  buildImgCol(data, panelId);
  buildDetailCol(data, panelId, isCur);
}

// ── Build image column with paginated gallery ────────────────────────
function buildImgCol(data, panelId){
  var col = document.getElementById(panelId+'-imgcol');
  if(!col) return;

  // Collect all images: main + sub_images (deduplicated)
  var imgs = [];
  if(data.image_url) imgs.push(data.image_url);
  (data.sub_images||[]).forEach(function(u){
    if(u && imgs.indexOf(u) < 0) imgs.push(u);
  });

  var mainId  = panelId+'-main';
  var gridId  = panelId+'-grid';
  var navId   = panelId+'-nav';
  var pageIdx = 0; // current page

  // Main image
  var mainHtml = imgs.length > 0
    ? '<img id="'+mainId+'" class="pp-main-img" src="'+esc(imgs[0])+'" alt="'+esc(data.sku)+'" '
      +'onerror="this.outerHTML=\'<div class=pp-img-placeholder>&#128247;</div>\'">'
    : '<div class="pp-img-placeholder">&#128247;</div>';

  // Price + channel
  var priceHtml = (data.price && data.price > 0)
    ? '<div class="pp-price">'+esc(data.currency)+' '+parseFloat(data.price).toFixed(2)+'</div>'
    : '';
  var chHtml = data.channel
    ? '<span class="pp-channel">'+esc(data.channel)+'</span>'
    : '';

  // Gallery
  var galleryHtml = '';
  if(imgs.length > 1){
    galleryHtml =
      '<div class="gallery-wrap">'
        +'<div class="gallery-label">&#128247; '+imgs.length+' images</div>'
        +'<div class="gallery-grid" id="'+gridId+'"></div>'
        +(imgs.length > PAGE_SIZE
          ? '<div class="gallery-nav" id="'+navId+'">'
              +'<button class="gn-btn" id="'+panelId+'-prev" onclick="galleryPage(\''+panelId+'\','+(pageIdx-1)+')">&#8592;</button>'
              +'<span class="gn-info" id="'+panelId+'-pginfo"></span>'
              +'<button class="gn-btn" id="'+panelId+'-next" onclick="galleryPage(\''+panelId+'\','+(pageIdx+1)+')">&#8594;</button>'
            +'</div>'
          : '')
      +'</div>';
  }

  col.innerHTML = mainHtml + priceHtml + chHtml + galleryHtml;

  // Store images on panel element for pagination
  document.getElementById(panelId).dataset.imgs = JSON.stringify(imgs);
  document.getElementById(panelId).dataset.mainId = mainId;

  // Render first page of thumbnails
  if(imgs.length > 1) renderGalleryPage(panelId, 0);
}

function renderGalleryPage(panelId, page){
  var panel = document.getElementById(panelId);
  if(!panel) return;
  var imgs   = JSON.parse(panel.dataset.imgs || '[]');
  var mainId = panel.dataset.mainId;
  var grid   = document.getElementById(panelId+'-grid');
  if(!grid) return;

  var totalPages = Math.ceil((imgs.length-1) / PAGE_SIZE);
  var start = 1 + page * PAGE_SIZE; // skip index 0 (main image)
  var end   = Math.min(start + PAGE_SIZE, imgs.length);
  panel.dataset.page = page;

  grid.innerHTML = imgs.slice(start, end).map(function(u, i){
    return '<img class="g-thumb" src="'+esc(u)+'" '
      +'onclick="swapMain(\''+esc(u)+'\',\''+mainId+'\',this,\''+panelId+'\')" '
      +'onerror="this.style.display=\'none\'" alt="'+(start+i)+'">';
  }).join('');

  // Update nav
  var prev = document.getElementById(panelId+'-prev');
  var next = document.getElementById(panelId+'-next');
  var info = document.getElementById(panelId+'-pginfo');
  if(prev) prev.disabled = (page === 0);
  if(next) next.disabled = (page >= totalPages - 1);
  if(info) info.textContent = (page+1)+'/'+totalPages;
}

function galleryPage(panelId, page){
  var panel = document.getElementById(panelId);
  if(!panel) return;
  var imgs  = JSON.parse(panel.dataset.imgs || '[]');
  var total = Math.ceil((imgs.length-1) / PAGE_SIZE);
  page = Math.max(0, Math.min(page, total-1));
  renderGalleryPage(panelId, page);
}

function swapMain(url, mainId, thumb, panelId){
  var main = document.getElementById(mainId);
  if(main) main.src = url;
  // Update active state within this panel only
  var panel = document.getElementById(panelId);
  if(panel) panel.querySelectorAll('.g-thumb').forEach(function(t){t.classList.remove('active');});
  thumb.classList.add('active');
}

// ── Build detail column ──────────────────────────────────────────────
function buildDetailCol(data, panelId, isCur){
  var det = document.getElementById(panelId+'-det');
  if(!det) return;
  var html = '';

  // Title
  if(data.title) html += '<div class="pp-title">'+esc(data.title)+'</div>';

  // Stock by location — highlight the selected location
  if(data.stock_by_loc && data.stock_by_loc.length > 0){
    html += '<div><div class="f-lbl" style="margin-bottom:6px;">Stock by Location</div>'
      +'<div class="stock-grid">';
    data.stock_by_loc.forEach(function(l){
      var isSelected = (l.location === LOC);
      var cls = stockClass(l.stock);
      var itemCls = 'stock-item' + (isSelected ? ' loc-selected' : '');
      // Extra bottom margin for selected item to make room for the "Selected" label
      var style = isSelected ? ' style="margin-bottom:12px;"' : '';
      html += '<div class="'+itemCls+'"'+style+'>'
        +'<div class="si-loc">'+esc(l.location)+'</div>'
        +'<div class="si-val '+cls+'">'+l.stock+'</div>'
        +'</div>';
    });
    html += '</div></div>';
  }

  // Velocity
  if(data.sold_7d !== undefined){
    html += '<div class="vel-row">'
      +'<div class="vel-item"><div class="vel-val">'+data.sold_7d+'</div><div class="vel-lbl">Sold 7 days</div></div>'
      +'<div class="vel-item"><div class="vel-val">'+data.sold_30d+'</div><div class="vel-lbl">Sold 30 days</div></div>'
      +'</div>';
  }

  // ASIN + Holder inline
  if(data.asin || data.holder){
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">';
    if(data.asin){
      // Use listing_url (site-specific: .co.uk / .de / .com) if available
      // Fallback: construct amazon.co.uk link only if no listing_url
      var amzUrl = data.listing_url || ('https://www.amazon.co.uk/dp/'+esc(data.asin));
      html += '<div class="field"><div class="f-lbl">ASIN</div>'
        +'<div class="f-val mono"><a href="'+esc(amzUrl)+'" target="_blank" class="alink">'+esc(data.asin)+' &#8599;</a></div></div>';
    }
    if(data.holder){
      html += '<div class="field"><div class="f-lbl">Holder</div>'
        +'<div class="f-val">'+esc(data.holder)+'</div></div>';
    }
    html += '</div>';
  }

  // Variations
  if(data.variations && data.variations.length > 0){
    html += '<div class="field"><div class="f-lbl">Variations</div><div class="var-wrap">';
    data.variations.forEach(function(v){
      html += '<span class="var-tag"><span class="vn">'+esc(v.name)+':</span> '+esc(v.value)+'</span>';
    });
    html += '</div></div>';
  }

  // Bullet points
  if(data.bullet_points && data.bullet_points.length > 0){
    html += '<div class="bullets-box"><div class="box-lbl">&#9679; Product Bullet Points</div>'
      +'<ul class="bullet-list">';
    data.bullet_points.forEach(function(b){
      html += '<li>'+esc(b)+'</li>';
    });
    html += '</ul></div>';
  }

  // Description
  if(data.description){
    html += '<div class="desc-box"><div class="box-lbl">Description</div>'
      +'<div class="desc-text">'+esc(data.description)+'</div></div>';
  }

  // Listing link
  if(data.listing_url){
    html += '<div><a href="'+esc(data.listing_url)+'" target="_blank" class="alink">'
      +'&#128279; View on '+esc(data.channel||'Amazon')+'</a></div>';
  }

  det.innerHTML = html;
}

// ── Checklist toggle ─────────────────────────────────────────────────
var clOpen = false;
function toggleChecklist(){
  clOpen = !clOpen;
  document.getElementById('cl-wrap').className = 'cl-wrap ' + (clOpen ? 'open' : 'closed');
  document.getElementById('cl-btn').textContent = clOpen ? '✕ Close' : '✎ Checklist';
}

// ── Build panels ─────────────────────────────────────────────────────
buildPanel(CUR, 'panel-cur', 'Current Listing (Out of Stock)', true);
buildPanel(SUG, 'panel-sug', 'Suggested Replacement', false);

// ── Set Amazon buttons ────────────────────────────────────────────────
var btnCur = document.getElementById('btn-cur');
var btnSug = document.getElementById('btn-sug');
if(btnCur){
  // listing_url is now site-specific (amazon.co.uk / .de / .com)
  var curUrl = CUR.listing_url || (CUR.asin ? 'https://www.amazon.co.uk/dp/'+CUR.asin : '');
  if(curUrl){ btnCur.href = curUrl; btnCur.classList.remove('off'); }
}
if(btnSug && SUG){
  var sugUrl = SUG.listing_url || (SUG.asin ? 'https://www.amazon.co.uk/dp/'+SUG.asin : '');
  if(sugUrl){ btnSug.href = sugUrl; btnSug.classList.remove('off'); }
}
</script>
</body>
</html>"""