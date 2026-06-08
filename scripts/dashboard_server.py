#!/usr/bin/env python3
"""
LEDs One - dashboard_server.py
Flask web dashboard with OpenClaw AI chat integration.
Port 8080

DATA SOURCE: PostgreSQL (stock_level db) — synced every 20 min by db_sync.py
- No direct MySQL queries. All data comes from PostgreSQL synced tables.
- Tables used: location_wise_inv_stock, order_item_info, orders,
               ebay_products, ph_mapping, sku_extras
- db_sync.py must run before data appears here (OpenClaw cron every 20 min)
"""
import os
import gzip
import json as _json
from datetime import datetime
from functools import wraps
import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool
import yaml
from flask import Flask, jsonify, request, Response

CONFIG_PATH = os.environ.get(
    "DASHBOARD_CONFIG",
    "/opt/openclaw/stock_level/config/stock_dashboard.yaml"
)

def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f)
    return {
        "postgres": {
            "host": "localhost",
            "port": 5432,
            "dbname": "stock_level",
            "user": "digit_web",
            "password": "digit123",
        },
        "stock_dashboard": {
            "days_critical_threshold": 7,
            "days_low_threshold": 21,
            "days_overstock_threshold": 90,
        }
    }

CFG = load_config()

# ── Connection pool — reuse TCP connections instead of creating per request ───
# min=2 connections always open; max=10 for concurrent requests
# Eliminates ~50-100ms TCP+SSL+auth overhead per API call
_PG_POOL = None

def _init_pool():
    global _PG_POOL
    if _PG_POOL is not None:
        return
    pg = CFG["postgres"]
    args = {
        "host":   pg.get("host",     "localhost"),
        "port":   pg.get("port",     5432),
        "dbname": pg.get("dbname",   "stock_level"),
        "user":   pg.get("user",     "digit_web"),
    }
    if pg.get("password"):
        args["password"] = pg["password"]
    _PG_POOL = pg_pool.ThreadedConnectionPool(2, 10, **args)

def pg_conn():
    """Get a connection from the pool. Caller must call pool.putconn() after use."""
    _init_pool()
    return _PG_POOL.getconn()

def pg_release(conn):
    """Return connection to pool."""
    if _PG_POOL and conn:
        _PG_POOL.putconn(conn)

# ── gzip compress JSON responses > 1KB ──────────────────────────────────────
def gzip_json(data):
    """
    Return gzip-compressed JSON response.
    3608 rows compresses from ~2.7MB to ~350KB (87% reduction).
    Critical for Cloudflare tunnel performance.
    """
    payload = _json.dumps(data, separators=(',', ':')).encode('utf-8')
    compressed = gzip.compress(payload, compresslevel=6)
    return Response(
        compressed,
        status=200,
        mimetype='application/json',
        headers={
            'Content-Encoding': 'gzip',
            'Vary': 'Accept-Encoding',
            'Content-Length': str(len(compressed)),
        }
    )

thresholds = CFG.get("stock_dashboard", {})
CRIT = thresholds.get("days_critical_threshold", 7)
LOW  = thresholds.get("days_low_threshold", 21)
OVER = thresholds.get("days_overstock_threshold", 90)

app = Flask(__name__)


def compute_velocity(s7, s14, s30):
    s7  = int(s7  or 0)
    s14 = int(s14 or 0)
    s30 = int(s30 or 0)
    if s7  >= 7: return round(s7  / 7,  2)
    if s14 >= 7: return round(s14 / 14, 2)
    if s30 >= 7: return round(s30 / 30, 2)
    return 0.0


def compute_status(stock, avg):
    if avg == 0:
        return "NO DATA", None
    days = max(0, int(stock / avg))
    if days <= CRIT: return "CRITICAL",   days
    if days <= LOW:  return "LOW",        days
    if days <= OVER: return "HEALTHY",    days
    return "OVERSTOCKED", days



def load_dashboard_data(marketplace="ALL"):
    """
    Read pre-computed data from dashboard_cache table (connection pool).
    Removes unused fields (sold_7d/14d/30d, item_id) to reduce JSON payload.
    3608 rows × 10 fields instead of 14 = ~30% less data per request.
    """
    conn = None
    try:
        conn = pg_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT sku, product_name, platform,
                       stock, inbound, reorder_point,
                       avg_per_day, days_remaining, status, holder
                FROM   dashboard_cache
                WHERE  location = %s
                ORDER  BY sort_order, sku
            """, (marketplace,))
            raw_rows = cur.fetchall()
    except Exception as e:
        raise RuntimeError(f"PostgreSQL query failed: {e}")
    finally:
        pg_release(conn)

    results = []
    for row in raw_rows:
        results.append({
            "sku":            row["sku"],
            "product_name":   row["product_name"] or row["sku"],
            "platform":       row["platform"]       or "",
            "stock":          int(row["stock"]          or 0),
            "inbound":        int(row["inbound"]        or 0),
            "reorder_point":  int(row["reorder_point"]  or 0),
            "avg_per_day":    float(row["avg_per_day"]  or 0),
            "days_remaining": row["days_remaining"],
            "status":         row["status"]             or "NO DATA",
            "holder":         row["holder"]             or "",
        })
    return results


@app.route("/api/stock")
def api_stock():
    marketplace     = request.args.get("marketplace", "ALL")
    holder_filter   = request.args.get("holder", "")
    status_filter   = request.args.get("status", "")
    search          = request.args.get("q", "").strip().lower()

    try:
        rows = load_dashboard_data(marketplace)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if holder_filter:
        rows = [r for r in rows if r["holder"] == holder_filter]
    if status_filter:
        rows = [r for r in rows if r["status"] == status_filter]
    if search:
        rows = [r for r in rows if
                search in r["sku"].lower() or
                search in (r.get("product_name") or "").lower() or
                search in (r.get("holder") or "").lower()]
    return gzip_json(rows)


@app.route("/api/summary")
def api_summary():
    marketplace = request.args.get("marketplace", "ALL")
    conn = None
    try:
        rows = load_dashboard_data(marketplace)
        # Get real synced_at from dashboard_cache
        conn = pg_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(synced_at) AT TIME ZONE 'UTC' FROM dashboard_cache WHERE location = %s", (marketplace,))
            r = cur.fetchone()
            ts = r[0] if r else None
            real_sync_ts = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts and hasattr(ts,"strftime") else datetime.utcnow().isoformat()+"Z"
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        pg_release(conn)

    counts = {"CRITICAL": 0, "LOW": 0, "HEALTHY": 0, "OVERSTOCKED": 0, "NO DATA": 0, "TOTAL": len(rows)}
    holder_counts   = {}
    holder_critical = {}

    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        h = r["holder"]
        holder_counts[h]   = holder_counts.get(h, 0) + 1
        if r["status"] == "CRITICAL":
            holder_critical[h] = holder_critical.get(h, 0) + 1

    holders = sorted(
        [{"name": k, "total": v, "critical": holder_critical.get(k, 0)}
         for k, v in holder_counts.items()],
        key=lambda x: x["name"]
    )
    return jsonify({
        "counts":     counts,
        "holders":    holders,
        "updated_at": real_sync_ts,
    })


@app.route("/api/marketplace-summary")
def api_marketplace_summary():
    """
    Returns per-marketplace counts from dashboard_cache — instant GROUP BY.
    Previously ran a complex CTE query on every call.
    Now reads pre-computed cache built at sync time.
    """
    conn = None
    try:
        conn = pg_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    location,
                    COUNT(*)                                        AS total,
                    COUNT(*) FILTER (WHERE status='CRITICAL')    AS critical,
                    COUNT(*) FILTER (WHERE status='LOW')         AS low,
                    COUNT(*) FILTER (WHERE status='HEALTHY')     AS healthy,
                    COUNT(*) FILTER (WHERE status='OVERSTOCKED') AS overstocked
                FROM dashboard_cache
                WHERE location IN ('UK','US','Germany','ALL')
                GROUP BY location
            """)
            rows = cur.fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        pg_release(conn)

    result = {
        "ALL":     {"CRITICAL":0,"LOW":0,"HEALTHY":0,"OVERSTOCKED":0,"NO DATA":0,"TOTAL":0},
        "UK":      {"CRITICAL":0,"LOW":0,"HEALTHY":0,"OVERSTOCKED":0,"NO DATA":0,"TOTAL":0},
        "US":      {"CRITICAL":0,"LOW":0,"HEALTHY":0,"OVERSTOCKED":0,"NO DATA":0,"TOTAL":0},
        "Germany": {"CRITICAL":0,"LOW":0,"HEALTHY":0,"OVERSTOCKED":0,"NO DATA":0,"TOTAL":0},
    }
    for row in rows:
        loc = row["location"]
        if loc not in result:
            continue
        crit  = int(row["critical"]    or 0)
        low   = int(row["low"]         or 0)
        hlth  = int(row["healthy"]     or 0)
        over  = int(row["overstocked"] or 0)
        total = int(row["total"]       or 0)
        result[loc].update({
            "CRITICAL":crit,"LOW":low,"HEALTHY":hlth,
            "OVERSTOCKED":over,"NO DATA":total-crit-low-hlth-over,"TOTAL":total
        })
    return jsonify(result)


@app.route("/api/all-summary")
def api_all_summary():
    """
    Combined endpoint — replaces loadMarketplaceSummary() + loadSummary() with ONE call.
    Returns: marketplace counts (UK/US/Germany/ALL) + holder list for active marketplace.
    Reduces 2 API calls to 1 on every page load and marketplace switch.
    """
    marketplace = request.args.get("marketplace", "ALL")
    conn = None
    try:
        conn = pg_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Both queries in same connection from pool
            cur.execute("""
                SELECT
                    location,
                    COUNT(*)                                        AS total,
                    COUNT(*) FILTER (WHERE status='CRITICAL')    AS critical,
                    COUNT(*) FILTER (WHERE status='LOW')         AS low,
                    COUNT(*) FILTER (WHERE status='HEALTHY')     AS healthy,
                    COUNT(*) FILTER (WHERE status='OVERSTOCKED') AS overstocked,
                    MAX(synced_at) AT TIME ZONE 'UTC'            AS synced_at
                FROM dashboard_cache
                WHERE location IN ('UK','US','Germany','ALL')
                GROUP BY location
            """)
            mp_rows = cur.fetchall()
            cur.execute("""
                SELECT holder, COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE status='CRITICAL') AS critical
                FROM dashboard_cache
                WHERE location = %s
                GROUP BY holder
                ORDER BY holder
            """, (marketplace,))
            holder_rows = cur.fetchall()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        pg_release(conn)

    # Build marketplace summary
    mp_result = {
        "ALL":     {"CRITICAL":0,"LOW":0,"HEALTHY":0,"OVERSTOCKED":0,"NO DATA":0,"TOTAL":0},
        "UK":      {"CRITICAL":0,"LOW":0,"HEALTHY":0,"OVERSTOCKED":0,"NO DATA":0,"TOTAL":0},
        "US":      {"CRITICAL":0,"LOW":0,"HEALTHY":0,"OVERSTOCKED":0,"NO DATA":0,"TOTAL":0},
        "Germany": {"CRITICAL":0,"LOW":0,"HEALTHY":0,"OVERSTOCKED":0,"NO DATA":0,"TOTAL":0},
    }
    # Extract real synced_at from dashboard_cache — used for stale detection
    real_sync_ts = ""
    for row in mp_rows:
        loc = row["location"]
        if loc not in mp_result: continue
        crit=int(row["critical"] or 0); low=int(row["low"] or 0)
        hlth=int(row["healthy"] or 0); over=int(row["overstocked"] or 0)
        total=int(row["total"] or 0)
        mp_result[loc].update({"CRITICAL":crit,"LOW":low,"HEALTHY":hlth,
            "OVERSTOCKED":over,"NO DATA":total-crit-low-hlth-over,"TOTAL":total})
        # synced_at is same for all locations — grab it once
        if not real_sync_ts and row.get("synced_at"):
            ts = row["synced_at"]
            real_sync_ts = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(ts, "strftime") else str(ts)

    # Build holder list
    holders = [{"name": r["holder"], "total": int(r["total"] or 0),
                "critical": int(r["critical"] or 0)} for r in holder_rows]

    active = mp_result.get(marketplace, mp_result["ALL"])
    return jsonify({
        "marketplace_summary": mp_result,
        "counts": active,
        "holders": holders,
        "updated_at": real_sync_ts or datetime.utcnow().isoformat() + "Z",
    })


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """OpenClaw AI chat endpoint"""
    import subprocess
    data    = request.get_json() or {}
    message = data.get("message", "").strip()
    if not message:
        return jsonify({"error": "No message"}), 400
    try:
        result = subprocess.run(
            ["openclaw", "agent", "--message", message, "--agent", "main"],
            capture_output=True, text=True, timeout=480
        )
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        lines = output.split("\n")
        skip_patterns = [
            "OpenClaw", "Built by", "lobsters", "WhatsApp", "Finally",
            "I can", "don't question", "passive", "man pages", "mac mini",
            "backlog", "privacy policy", "taste", "emoji", "env is showing"
        ]
        response_lines = []
        capture = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if capture:
                    response_lines.append("")
                continue
            if stripped in ["◇", "◆", "|"]:
                capture = True
                continue
            if any(p.lower() in stripped.lower() for p in skip_patterns):
                continue
            if capture:
                response_lines.append(line)
            elif stripped and not any(p.lower() in stripped.lower() for p in skip_patterns):
                capture = True
                response_lines.append(line)

        response = "\n".join(response_lines).strip() or output
        return jsonify({"response": response})
    except subprocess.TimeoutExpired:
        return jsonify({"response": "OpenClaw is taking longer than expected. Try again."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/remap")
def remap_page():
    """Serve remap page from separate remap_server module."""
    try:
        from remap_server import get_remap_html
        return get_remap_html()
    except ImportError:
        return "<h2>remap_server.py not found in the same directory.</h2>", 500


@app.route("/api/remap-suggestions")
def api_remap_suggestions_proxy():
    """Proxy remap API — delegates to remap_server module."""
    try:
        from remap_server import remap_suggestions
        return remap_suggestions()
    except ImportError:
        return jsonify({"error": "remap_server.py not found"}), 500


@app.route("/api/remap-summary")
def api_remap_summary_proxy():
    """Proxy remap summary — one lightweight call replaces 3 CTE queries."""
    try:
        from remap_server import remap_summary
        return remap_summary()
    except ImportError:
        return jsonify({"error": "remap_server.py not found"}), 500


@app.route("/product-detail-card")
def product_detail_card_page():
    """Full landscape product detail page — same-tab navigation."""
    try:
        from product_detail_card import get_detail_card_html
        return get_detail_card_html()
    except ImportError:
        return "<h2>product_detail_card.py not found in the same directory.</h2>", 500


@app.route("/api/product-detail")
def api_product_detail_proxy():
    """Proxy product detail API — delegates to remap_server module."""
    try:
        from remap_server import product_detail
        return product_detail()
    except ImportError:
        return jsonify({"error": "remap_server.py not found"}), 500


@app.route("/")
def index():
    return HTML


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LEDs One &mdash; Stock Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=DM+Mono&display=swap" rel="stylesheet">
<style>
:root{--bg:#0f1117;--surface:#1a1d27;--border:#2a2d3a;--text:#e8eaf0;--muted:#6b7280;
--red:#ef4444;--orange:#f97316;--green:#22c55e;--blue:#3b82f6;--grey:#6b7280;--chat-w:360px;
--shadow:0 2px 12px rgba(0,0,0,.35);--shadow-hover:0 4px 20px rgba(0,0,0,.5);}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:"DM Sans",sans-serif;display:flex;flex-direction:column;height:100vh;overflow:hidden;}
header{padding:14px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;position:sticky;top:0;background:var(--bg);z-index:100;}
header h1{font-size:1.2rem;font-weight:700;}
.header-right{display:flex;align-items:center;gap:10px;}
header .ts{font-size:0.72rem;color:var(--muted);font-family:"DM Mono",monospace;}
.chat-toggle{background:#1e3a5f;border:1px solid #3b82f6;color:#93c5fd;border-radius:8px;padding:6px 14px;font-size:0.78rem;cursor:pointer;font-family:"DM Sans",sans-serif;transition:background .15s;}
.chat-toggle:hover{background:#2a4f80;}
/* ── Marketplace dropdown ── */
.mp-wrap{position:relative;}
.mp-btn{background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:8px;
  padding:6px 12px;font-size:0.78rem;font-family:"DM Sans",sans-serif;cursor:pointer;
  display:flex;align-items:center;gap:6px;transition:border-color .15s;}
.mp-btn:hover,.mp-btn.open{border-color:#3b82f6;color:#93c5fd;}
.mp-btn .mp-arrow{font-size:0.6rem;transition:transform .2s;}
.mp-btn.open .mp-arrow{transform:rotate(180deg);}
.mp-dropdown{display:none;position:absolute;right:0;top:calc(100% + 6px);
  background:var(--surface);border:1px solid var(--border);border-radius:10px;
  min-width:300px;z-index:200;padding:8px;box-shadow:0 8px 28px rgba(0,0,0,.5);}
.mp-dropdown.open{display:block;}
.mp-item{display:flex;align-items:center;justify-content:space-between;
  padding:8px 10px;border-radius:7px;cursor:pointer;transition:background .12s;gap:10px;}
.mp-item:hover{background:rgba(255,255,255,.05);}
.mp-item.active{background:#1e3a5f;}
.mp-item .mp-name{font-size:0.8rem;font-weight:600;flex:1;}
.mp-item.active .mp-name{color:#93c5fd;}
.mp-badges{display:flex;gap:5px;align-items:center;}
.mp-badge{font-size:0.6rem;font-weight:700;padding:2px 7px;border-radius:999px;font-family:"DM Mono",monospace;}
.mp-badge.crit{background:rgba(239,68,68,.18);color:var(--red);}
.mp-badge.low{background:rgba(249,115,22,.18);color:var(--orange);}
.mp-badge.total{background:rgba(255,255,255,.07);color:var(--muted);}
.mp-divider{height:1px;background:var(--border);margin:4px 0;}
/* ── Marketplace location cards row (All + UK + US + Germany) ── */
.mp-summary-row{display:grid;grid-template-columns:1fr repeat(3,1.4fr);gap:12px;margin-bottom:16px;}
/* All Markets card — first column, compact */
.mp-all-card{background:linear-gradient(135deg,#1a1f35 0%,#1e2540 100%);
  border:1px solid rgba(167,139,250,.25);border-radius:12px;
  padding:14px 16px;cursor:pointer;transition:border-color .15s,box-shadow .15s;
  display:flex;flex-direction:column;justify-content:center;align-items:flex-start;
  box-shadow:var(--shadow);}
.mp-all-card:hover{border-color:rgba(167,139,250,.5);box-shadow:var(--shadow-hover);}
.mp-all-card.active{border-color:#a78bfa;box-shadow:0 0 0 1px #a78bfa,var(--shadow-hover);}
.mp-all-card .mp-all-label{font-size:0.6rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#a78bfa;margin-bottom:6px;}
.mp-all-card .mp-all-count{font-size:2rem;font-weight:800;font-family:"DM Mono",monospace;color:#e2d9f3;line-height:1;}
.mp-all-card .mp-all-sub{font-size:0.62rem;color:rgba(167,139,250,.6);margin-top:4px;}
/* Location cards (UK / US / Germany) */
.mp-summary-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:14px 16px;cursor:pointer;transition:border-color .15s,box-shadow .15s,background .15s;position:relative;
  box-shadow:var(--shadow);}
.mp-summary-card:hover{border-color:#3d4255;box-shadow:var(--shadow-hover);}
.mp-summary-card.active{border-color:#3b82f6;background:#0f1829;box-shadow:0 0 0 1px #3b82f6,var(--shadow-hover);transition:none;}
.mp-sc-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.mp-sc-name{font-size:0.75rem;font-weight:700;color:var(--text);letter-spacing:.01em;}
.mp-summary-card.active .mp-sc-name{color:#93c5fd;}
.mp-sc-total-pill{font-size:0.6rem;font-weight:700;font-family:"DM Mono",monospace;
  background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);
  color:var(--muted);padding:2px 8px;border-radius:999px;}
.mp-sc-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;}
.mp-sc-stat{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);
  border-radius:7px;padding:6px 4px;text-align:center;}
.mp-sc-stat .sv{font-size:1rem;font-weight:700;font-family:"DM Mono",monospace;line-height:1.1;}
.mp-sc-stat .sl{font-size:0.52rem;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-top:3px;}
.sv.crit{color:var(--red);}
.sv.low-c{color:var(--orange);}
.sv.hlth{color:var(--green);}
.sv.over{color:var(--blue);}
/* ── Status cards ── */
.main-wrap{display:flex;flex:1;overflow:hidden;height:calc(100vh - 52px);position:relative;}
.dashboard{flex:1;padding:20px 24px;overflow-y:auto;min-height:0;scrollbar-width:thin;scrollbar-color:var(--border) transparent;}
.dashboard::-webkit-scrollbar{width:4px;}
.dashboard::-webkit-scrollbar-track{background:transparent;}
.dashboard::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px;}
.dashboard::-webkit-scrollbar-thumb:hover{background:#4a4d5a;}
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:14px;}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:13px;
  cursor:pointer;transition:box-shadow .15s,border-color .15s;}
.card:hover{transform:translateY(-1px);}
.card.active-card{border-color:currentColor;transition:none;}
.card .label{font-size:0.62rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:5px;}
.card .count{font-size:1.7rem;font-weight:700;font-family:"DM Mono",monospace;}
.card.c-critical{color:var(--red);   box-shadow:-3px 3px 0px rgba(239,68,68,.6);}
.card.c-critical:hover{box-shadow:-4px 4px 0px rgba(239,68,68,.85);}
.card.c-low{color:var(--orange);     box-shadow:-3px 3px 0px rgba(249,115,22,.6);}
.card.c-low:hover{box-shadow:-4px 4px 0px rgba(249,115,22,.85);}
.card.c-healthy{color:var(--green);  box-shadow:-3px 3px 0px rgba(34,197,94,.6);}
.card.c-healthy:hover{box-shadow:-4px 4px 0px rgba(34,197,94,.85);}
.card.c-over{color:var(--blue);      box-shadow:-3px 3px 0px rgba(59,130,246,.6);}
.card.c-over:hover{box-shadow:-4px 4px 0px rgba(59,130,246,.85);}
.card.c-nodata{color:var(--grey);    box-shadow:-3px 3px 0px rgba(107,114,128,.6);}
.card.c-nodata:hover{box-shadow:-4px 4px 0px rgba(107,114,128,.85);}
.card.c-total{color:var(--text);     box-shadow:-3px 3px 0px rgba(232,234,240,.3);}
.card.c-total:hover{box-shadow:-4px 4px 0px rgba(232,234,240,.5);}
/* ── Holder filter buttons ── */
.holders-wrap{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px;}
.hbtn{background:var(--surface);border:1px solid var(--border);border-radius:8px;
  padding:5px 13px;font-size:0.73rem;color:var(--muted);cursor:pointer;
  transition:all .15s;white-space:nowrap;font-family:"DM Sans",sans-serif;
  display:inline-flex;align-items:center;gap:4px;}
.hbtn:hover{border-color:#4a4d5a;color:var(--text);background:rgba(255,255,255,.04);}
.hbtn.active{background:#1a2744;border-color:#3b82f6;color:#93c5fd;font-weight:600;}
.hbtn .bc{background:var(--red);color:#fff;border-radius:999px;padding:1px 6px;
  font-size:0.6rem;font-weight:700;margin-left:2px;line-height:1.4;}
.hbtn .tc{background:rgba(255,255,255,.07);color:var(--muted);border-radius:999px;
  padding:1px 6px;font-size:0.6rem;margin-left:2px;line-height:1.4;}
.hbtn.active .tc{background:rgba(59,130,246,.15);color:#93c5fd;}
/* ── Toolbar ── */
.toolbar{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;}
.toolbar input{flex:1;min-width:180px;background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 13px;font-size:0.83rem;font-family:"DM Sans",sans-serif;}
.toolbar input:focus{outline:none;border-color:#3b82f6;}
.toolbar select{background:var(--surface);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 13px;font-size:0.83rem;font-family:"DM Sans",sans-serif;cursor:pointer;}
.toolbar select:focus{outline:none;border-color:#3b82f6;}
.rc{font-size:0.7rem;color:var(--muted);font-family:"DM Mono",monospace;padding:4px 0;}
/* ── Table ── */
.tw{overflow-x:auto;border-radius:10px;border:1px solid var(--border);scrollbar-width:none;}
.tw::-webkit-scrollbar{display:none;}
table{width:100%;border-collapse:collapse;font-size:0.78rem;table-layout:fixed;}
thead th{background:var(--surface);padding:9px 11px;text-align:left;font-size:0.62rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border);white-space:nowrap;overflow:hidden;}
/* Column widths — fixed layout */
th:nth-child(1){width:13%;}  /* SKU */
th:nth-child(2){width:27%;}  /* Product Name */
th:nth-child(3){width:7%;}   /* Platform */
th:nth-child(4){width:6%;}   /* Stock */
th:nth-child(5){width:5%;}   /* Inbound */
th:nth-child(6){width:6%;}   /* Reorder */
th:nth-child(7){width:6%;}   /* Avg/Day */
th:nth-child(8){width:6%;}   /* Days Left */
th:nth-child(9){width:8%;}   /* Status */
th:nth-child(10){width:11%;} /* Holder */
th:nth-child(11){width:5%;}  /* Remap */
tbody tr{border-bottom:1px solid var(--border);transition:background .1s;}
tbody tr:hover{background:rgba(255,255,255,.025);}
td{padding:8px 11px;vertical-align:middle;overflow:hidden;}
/* SKU — wrap fully, no truncation */
td.sk{font-family:"DM Mono",monospace;font-size:0.7rem;word-break:break-all;white-space:normal;line-height:1.4;}
/* Product Name — ellipsis with tooltip */
td.nm{white-space:normal;word-break:break-word;}
/* ── Status badges with dot ── */
.badge{display:inline-flex;align-items:center;gap:5px;padding:3px 9px;border-radius:999px;font-size:0.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;}
.badge::before{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;flex-shrink:0;}
.badge.CRITICAL{background:rgba(239,68,68,.13);color:var(--red);border:1px solid rgba(239,68,68,.25);}
.badge.CRITICAL::before{background:var(--red);box-shadow:0 0 4px rgba(239,68,68,.6);}
.badge.LOW{background:rgba(249,115,22,.13);color:var(--orange);border:1px solid rgba(249,115,22,.25);}
.badge.LOW::before{background:var(--orange);box-shadow:0 0 4px rgba(249,115,22,.6);}
.badge.HEALTHY{background:rgba(34,197,94,.13);color:var(--green);border:1px solid rgba(34,197,94,.25);}
.badge.HEALTHY::before{background:var(--green);box-shadow:0 0 4px rgba(34,197,94,.6);}
.badge.OVERSTOCKED{background:rgba(59,130,246,.13);color:var(--blue);border:1px solid rgba(59,130,246,.25);}
.badge.OVERSTOCKED::before{background:var(--blue);box-shadow:0 0 4px rgba(59,130,246,.6);}
.badge.NO-DATA{background:rgba(107,114,128,.13);color:var(--grey);border:1px solid rgba(107,114,128,.2);}
.badge.NO-DATA::before{background:var(--grey);}
/* ── Holder badge (table) — fixed uniform size ── */
.holder-badge{display:inline-flex;align-items:center;justify-content:flex-start;gap:5px;
  padding:4px 10px;border-radius:20px;
  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);
  font-size:0.67rem;font-weight:600;color:var(--text);
  width:100%;max-width:140px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  transition:background .15s;}
.holder-badge .h-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;opacity:0.9;}
/* ── Platform badges ── */
.plat{display:inline-block;padding:2px 7px;border-radius:4px;font-size:0.6rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;}
.plat-amazon{background:rgba(255,153,0,.15);color:#ff9900;}
.plat-ebay{background:rgba(59,130,246,.12);color:#3b82f6;}
.plat-shopify{background:rgba(34,197,94,.12);color:#22c55e;}
.plat-unknown{background:rgba(107,114,128,.12);color:var(--muted);}
.loading{text-align:center;padding:40px;color:var(--muted);}
/* ── Professional loading states ── */
@keyframes shimmer{0%{background-position:-1000px 0}100%{background-position:1000px 0}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
.skeleton-row{height:38px;border-radius:6px;margin-bottom:4px;
  background:linear-gradient(90deg,var(--surface) 25%,rgba(255,255,255,.06) 50%,var(--surface) 75%);
  background-size:1000px 100%;animation:shimmer 1.6s infinite linear;}
.loading-wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:48px 24px;gap:14px;}
.loading-spinner{width:28px;height:28px;border:3px solid rgba(255,255,255,.08);
  border-top-color:var(--blue);border-radius:50%;animation:spin .75s linear infinite;}
.loading-text{font-size:0.75rem;color:var(--muted);font-family:"DM Mono",monospace;}
.loading-steps{display:flex;flex-direction:column;gap:5px;min-width:200px;}
.loading-step{display:flex;align-items:center;gap:8px;font-size:0.7rem;color:var(--muted);}
.loading-step .ls-dot{width:6px;height:6px;border-radius:50%;background:var(--border);flex-shrink:0;}
.loading-step.active .ls-dot{background:var(--blue);box-shadow:0 0 6px rgba(59,130,246,.6);}
.loading-step.done .ls-dot{background:var(--green);}
.loading-step.done{color:rgba(34,197,94,.6);}
.loading-step.active{color:var(--text);}
.fade-in{animation:fadeIn .3s ease both;}
.ri{font-size:0.7rem;color:var(--muted);text-align:right;margin-top:8px;}
/* Chat Panel */
.chat-panel{width:var(--chat-w);min-width:var(--chat-w);background:var(--surface);border-left:1px solid var(--border);display:flex;flex-direction:column;transition:all .25s;}
.chat-panel.hidden{width:0;min-width:0;overflow:hidden;border-left:none;}
.chat-header{padding:14px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}
.chat-header h3{font-size:0.9rem;font-weight:600;}
.chat-header span{font-size:0.68rem;color:var(--green);}
.chat-messages{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px;min-height:0;max-height:100%;}
.msg{max-width:90%;padding:9px 12px;border-radius:10px;font-size:0.78rem;line-height:1.5;word-break:break-word;}
.msg.user{background:#1e3a5f;color:#93c5fd;align-self:flex-end;border-bottom-right-radius:3px;}
.msg.bot{background:var(--bg);border:1px solid var(--border);color:var(--text);align-self:flex-start;border-bottom-left-radius:3px;}
.msg.bot.loading-msg{color:var(--muted);font-style:italic;}
.chat-input-wrap{padding:12px;border-top:1px solid var(--border);display:flex;gap:8px;flex-shrink:0;position:relative;z-index:1;}
.chat-input-wrap input{flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px 12px;font-size:0.8rem;font-family:"DM Sans",sans-serif;}
.chat-input-wrap input:focus{outline:none;border-color:#3b82f6;}
.chat-input-wrap button{background:#3b82f6;border:none;color:#fff;border-radius:8px;padding:8px 14px;font-size:0.78rem;cursor:pointer;font-family:"DM Sans",sans-serif;white-space:nowrap;}
.chat-input-wrap button:hover{background:#2563eb;}
.chat-input-wrap button:disabled{background:#374151;cursor:not-allowed;}
.chat-suggestions{padding:0 12px 10px;display:flex;flex-wrap:wrap;gap:6px;flex-shrink:0;}
.sugg{background:var(--bg);border:1px solid var(--border);color:var(--muted);border-radius:999px;padding:3px 10px;font-size:0.68rem;cursor:pointer;transition:all .15s;}
.sugg:hover{border-color:#3b82f6;color:var(--text);}
@media(max-width:900px){.cards{grid-template-columns:repeat(3,1fr);}
.mp-summary-row{grid-template-columns:1fr 1fr;}
.chat-panel{position:fixed;right:0;top:0;bottom:0;z-index:200;}
.chat-panel.hidden{transform:translateX(100%);width:var(--chat-w);min-width:var(--chat-w);}}
</style>
</head>
<body>
<header>
  <h1>&#9889; LEDs One &middot; Stock Dashboard</h1>
  <div class="header-right">
    <span class="ts" id="last-updated">Loading&hellip;</span>
    <a href="/remap" style="background:#1a2f1a;border:1px solid #22c55e;color:#4ade80;border-radius:8px;padding:6px 14px;font-size:0.78rem;text-decoration:none;font-family:'DM Sans',sans-serif;">&#8635; Remap</a>
    <!-- Marketplace dropdown button -->
    <div class="mp-wrap" id="mp-wrap">
      <button class="mp-btn" id="mp-btn" onclick="toggleMpDropdown()">
        <span id="mp-label">All Marketplaces</span>
        <span class="mp-arrow">&#9660;</span>
      </button>
      <div class="mp-dropdown" id="mp-dropdown">
        <div class="mp-item active" data-mp="ALL" onclick="selectMarketplace('ALL',this)">
          <span class="mp-name">All Marketplaces</span>
          <div class="mp-badges">
            <span class="mp-badge crit" id="dd-ALL-crit">-</span>
            <span class="mp-badge low"  id="dd-ALL-low">-</span>
            <span class="mp-badge total" id="dd-ALL-total">-</span>
          </div>
        </div>
        <div class="mp-divider"></div>
        <div class="mp-item" data-mp="UK" onclick="selectMarketplace('UK',this)">
          <span class="mp-name">&#127468;&#127463; UK</span>
          <div class="mp-badges">
            <span class="mp-badge crit"  id="dd-UK-crit">-</span>
            <span class="mp-badge low"   id="dd-UK-low">-</span>
            <span class="mp-badge total" id="dd-UK-total">-</span>
          </div>
        </div>
        <div class="mp-item" data-mp="US" onclick="selectMarketplace('US',this)">
          <span class="mp-name">&#127482;&#127480; US</span>
          <div class="mp-badges">
            <span class="mp-badge crit"  id="dd-US-crit">-</span>
            <span class="mp-badge low"   id="dd-US-low">-</span>
            <span class="mp-badge total" id="dd-US-total">-</span>
          </div>
        </div>
        <div class="mp-item" data-mp="Germany" onclick="selectMarketplace('Germany',this)">
          <span class="mp-name">&#127465;&#127466; Germany</span>
          <div class="mp-badges">
            <span class="mp-badge crit"  id="dd-Germany-crit">-</span>
            <span class="mp-badge low"   id="dd-Germany-low">-</span>
            <span class="mp-badge total" id="dd-Germany-total">-</span>
          </div>
        </div>
      </div>
    </div>
    <button class="chat-toggle" onclick="toggleChat()">&#129302; AI Chat</button>
  </div>
</header>

<div class="main-wrap">
  <div class="dashboard" id="dashboard">

    <!-- Marketplace cards: All Markets + UK + US + Germany -->
    <div class="mp-summary-row" id="mp-summary-row">

      <!-- All Markets — first card -->
      <div class="mp-all-card" id="card-ALL-MP" onclick="selectMarketplace('ALL',null)">
        <div class="mp-all-label">All Markets</div>
        <div class="mp-all-count" id="c-all-mp">&#8734;</div>
        <div class="mp-all-sub">All locations combined</div>
      </div>

      <!-- UK -->
      <div class="mp-summary-card" id="msc-UK" onclick="selectMarketplace('UK',null)">
        <div class="mp-sc-header">
          <div class="mp-sc-name">&#127468;&#127463; United Kingdom</div>
          <div class="mp-sc-total-pill" id="msc-UK-total">-</div>
        </div>
        <div class="mp-sc-stats">
          <div class="mp-sc-stat"><div class="sv crit" id="msc-UK-crit">-</div><div class="sl">Critical</div></div>
          <div class="mp-sc-stat"><div class="sv low-c" id="msc-UK-low">-</div><div class="sl">Low</div></div>
          <div class="mp-sc-stat"><div class="sv hlth" id="msc-UK-healthy">-</div><div class="sl">Healthy</div></div>
          <div class="mp-sc-stat"><div class="sv over" id="msc-UK-over">-</div><div class="sl">Overstock</div></div>
        </div>
      </div>

      <!-- US -->
      <div class="mp-summary-card" id="msc-US" onclick="selectMarketplace('US',null)">
        <div class="mp-sc-header">
          <div class="mp-sc-name">&#127482;&#127480; United States</div>
          <div class="mp-sc-total-pill" id="msc-US-total">-</div>
        </div>
        <div class="mp-sc-stats">
          <div class="mp-sc-stat"><div class="sv crit" id="msc-US-crit">-</div><div class="sl">Critical</div></div>
          <div class="mp-sc-stat"><div class="sv low-c" id="msc-US-low">-</div><div class="sl">Low</div></div>
          <div class="mp-sc-stat"><div class="sv hlth" id="msc-US-healthy">-</div><div class="sl">Healthy</div></div>
          <div class="mp-sc-stat"><div class="sv over" id="msc-US-over">-</div><div class="sl">Overstock</div></div>
        </div>
      </div>

      <!-- Germany -->
      <div class="mp-summary-card" id="msc-Germany" onclick="selectMarketplace('Germany',null)">
        <div class="mp-sc-header">
          <div class="mp-sc-name">&#127465;&#127466; Germany</div>
          <div class="mp-sc-total-pill" id="msc-Germany-total">-</div>
        </div>
        <div class="mp-sc-stats">
          <div class="mp-sc-stat"><div class="sv crit" id="msc-Germany-crit">-</div><div class="sl">Critical</div></div>
          <div class="mp-sc-stat"><div class="sv low-c" id="msc-Germany-low">-</div><div class="sl">Low</div></div>
          <div class="mp-sc-stat"><div class="sv hlth" id="msc-Germany-healthy">-</div><div class="sl">Healthy</div></div>
          <div class="mp-sc-stat"><div class="sv over" id="msc-Germany-over">-</div><div class="sl">Overstock</div></div>
        </div>
      </div>

    </div>

    <!-- Status cards for selected marketplace -->
    <div class="cards">
      <div class="card c-critical" id="card-CRITICAL" onclick="filterStatus('CRITICAL')">
        <div class="label">Critical</div><div class="count" id="c-critical">-</div></div>
      <div class="card c-low" id="card-LOW" onclick="filterStatus('LOW')">
        <div class="label">Low</div><div class="count" id="c-low">-</div></div>
      <div class="card c-healthy" id="card-HEALTHY" onclick="filterStatus('HEALTHY')">
        <div class="label">Healthy</div><div class="count" id="c-healthy">-</div></div>
      <div class="card c-over" id="card-OVERSTOCKED" onclick="filterStatus('OVERSTOCKED')">
        <div class="label">Overstocked</div><div class="count" id="c-over">-</div></div>
      <div class="card c-nodata" id="card-NODATA" onclick="filterStatus('NO DATA')">
        <div class="label">No Data</div><div class="count" id="c-nodata">-</div></div>
      <div class="card c-total" id="card-ALL" onclick="filterStatus('')">
        <div class="label">Total SKUs</div><div class="count" id="c-total">-</div></div>
    </div>

    <div class="holders-wrap" id="holders-wrap">
      <button class="hbtn active" data-h="ALL" onclick="filterHolder(this)">All Holders</button>
    </div>
    <div class="toolbar">
      <input type="search" id="qbox" placeholder="Search SKU or product name..." oninput="applyFilters()">
      <select id="sf" onchange="onStatusDrop()">
        <option value="">All Statuses</option>
        <option value="CRITICAL">Critical</option>
        <option value="LOW">Low</option>
        <option value="HEALTHY">Healthy</option>
        <option value="OVERSTOCKED">Overstocked</option>
        <option value="NO DATA">No Data</option>
      </select>
    </div>
    <div class="rc" id="rc"></div>
    <div class="tw">
      <table>
        <thead><tr>
          <th>SKU</th><th>Product Name</th><th>Platform</th>
          <th>Stock</th><th>Inbound</th><th>Reorder PT</th>
          <th>Avg/Day</th><th>Days Left</th><th>Status</th><th>Holder</th><th>Remap</th>
        </tr></thead>
        <tbody id="tb"><tr><td colspan="11" class="loading">Loading...</td></tr></tbody>
      </table>
    </div>
    <div class="ri">Updates every 20 minutes &middot; <span id="nr"></span></div>
  </div>

  <!-- AI Chat Panel -->
  <div class="chat-panel hidden" id="chat-panel">
    <div class="chat-header">
      <div>
        <h3>&#129302; OpenClaw AI</h3>
        <span>&#9679; Stock Dashboard Assistant</span>
      </div>
      <button onclick="toggleChat()" style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:1.1rem;">&#10005;</button>
    </div>
    <div class="chat-messages" id="chat-messages">
      <div class="msg bot">Hi! I am OpenClaw AI for LEDs One. Ask me about stock levels, critical SKUs, or run the dashboard sync.</div>
    </div>
    <div class="chat-suggestions">
      <span class="sugg" onclick="sendSugg(this)">run stock dashboard</span>
      <span class="sugg" onclick="sendSugg(this)">show critical SKUs</span>
      <span class="sugg" onclick="sendSugg(this)">stock summary</span>
      <span class="sugg" onclick="sendSugg(this)">top selling SKUs</span>
      <span class="sugg" onclick="sendSugg(this)">check sync logs</span>
    </div>
    <div class="chat-input-wrap">
      <input type="text" id="chat-in" placeholder="Ask about stock..." onkeydown="if(event.key==='Enter')sendChat()">
      <button id="chat-btn" onclick="sendChat()">Send</button>
    </div>
  </div>
</div>

<script>
var allRows=[], activeStatus='', activeHolder='ALL', activeMarketplace='ALL';
var refreshIn=1200, chatOpen=false, mpDropOpen=false;
var cachedHolders=[];

// ── sessionStorage cache — persists data across page navigations ──────────────
// Key insight: cache validity is based on SERVER sync time, not browser save time.
// /api/all-summary returns updated_at (the server's last sync timestamp).
// If server updated_at > cache saved_at → new data available → invalidate cache.
// This prevents showing stale data after a 20-min db_sync.py run.
//
// Flow:
//   1. Page opens → check sessionStorage cache
//   2. If cache exists → render instantly (show cached rows immediately)
//   3. Call /api/all-summary → compare response updated_at vs cache saved_at
//   4. If server is newer → silently refresh data in background
//   5. If server matches → cache is still valid, keep using it
var CACHE_KEY = 'ledsone_dash_cache';
var CACHE_TS_KEY = 'ledsone_dash_cache_ts';
var CACHE_SYNC_KEY = 'ledsone_dash_sync_ts'; // server's last sync timestamp
var CACHE_TTL = 25 * 60 * 1000; // 25 min safety margin (sync every 20 min)

function _saveCache(cache, serverSyncTs){
  try{
    sessionStorage.setItem(CACHE_KEY, JSON.stringify(cache));
    sessionStorage.setItem(CACHE_TS_KEY, Date.now().toString());
    if(serverSyncTs) sessionStorage.setItem(CACHE_SYNC_KEY, serverSyncTs);
  }catch(e){}
}
function _loadCache(){
  try{
    var ts = parseInt(sessionStorage.getItem(CACHE_TS_KEY) || '0');
    if(Date.now() - ts > CACHE_TTL) return null; // hard expiry — 25 min max
    var raw = sessionStorage.getItem(CACHE_KEY);
    return raw ? JSON.parse(raw) : null;
  }catch(e){ return null; }
}
function _getCachedSyncTs(){
  try{ return sessionStorage.getItem(CACHE_SYNC_KEY) || ''; }catch(e){ return ''; }
}
function _isCacheStale(serverUpdatedAt){
  // Returns true if server has newer data than what is in our cache
  var cachedSyncTs = _getCachedSyncTs();
  if(!cachedSyncTs) return true; // no sync ts saved — treat as stale
  // Compare ISO strings — lexicographic comparison works for ISO 8601
  return serverUpdatedAt > cachedSyncTs;
}

// Try restoring cache from sessionStorage
var _stored = _loadCache();
var dataCache = _stored || {ALL:null, UK:null, US:null, Germany:null};
var cacheLoading = {ALL:false, UK:false, US:false, Germany:false};

function E(id){return document.getElementById(id);}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

function platClass(p){
  p=(p||'').toLowerCase();
  if(p==='amazon') return 'plat plat-amazon';
  if(p==='ebay')   return 'plat plat-ebay';
  if(p==='shopify')return 'plat plat-shopify';
  return 'plat plat-unknown';
}

// ── Marketplace dropdown ────────────────────────────────────────────────────
function toggleMpDropdown(){
  mpDropOpen=!mpDropOpen;
  E('mp-btn').classList.toggle('open', mpDropOpen);
  E('mp-dropdown').classList.toggle('open', mpDropOpen);
}

document.addEventListener('click', function(e){
  if(!E('mp-wrap').contains(e.target) && mpDropOpen){
    mpDropOpen=false;
    E('mp-btn').classList.remove('open');
    E('mp-dropdown').classList.remove('open');
  }
});

function selectMarketplace(mp, itemEl){
  activeMarketplace = mp;
  activeHolder = 'ALL';
  activeStatus = '';
  E('sf').value = '';
  E('qbox').value = '';
  document.querySelectorAll('.card').forEach(function(c){c.classList.remove('active-card');});

  // Update dropdown button label
  var labels = {'ALL':'All Marketplaces','UK':'🇬🇧 UK','US':'🇺🇸 US','Germany':'🇩🇪 Germany'};
  E('mp-label').textContent = labels[mp] || mp;

  // Update dropdown active state
  document.querySelectorAll('.mp-item').forEach(function(el){
    el.classList.toggle('active', el.dataset.mp === mp);
  });

  // Update marketplace summary cards active state
  ['UK','US','Germany'].forEach(function(m){
    var card = E('msc-'+m);
    if(card) card.classList.toggle('active', m === mp);
  });
  // All Markets card
  var allCard=E('card-ALL-MP');
  if(allCard) allCard.classList.toggle('active', mp==='ALL');

  // Close dropdown
  mpDropOpen=false;
  E('mp-btn').classList.remove('open');
  E('mp-dropdown').classList.remove('open');

  // Single combined call — loadAllSummary handles counts + stale check
  // loadData handles table rows — uses cache if available (no extra API call)
  loadAllSummary();
  loadData();
}

// ── ONE combined call replaces loadMarketplaceSummary + loadSummary ──────────
// Reduces 2 API round-trips to 1 — critical for Cloudflare tunnel latency
async function loadAllSummary(){
  try{
    var url = '/api/all-summary?marketplace='+encodeURIComponent(activeMarketplace);
    var d = await (await fetch(url)).json();

    // Fill marketplace summary cards + dropdown badges
    var mp = d.marketplace_summary || {};
    ['UK','US','Germany','ALL'].forEach(function(m){
      var c = mp[m] || {};
      if(E('dd-'+m+'-crit'))  E('dd-'+m+'-crit').textContent  = c.CRITICAL||0;
      if(E('dd-'+m+'-low'))   E('dd-'+m+'-low').textContent   = c.LOW||0;
      if(E('dd-'+m+'-total')) E('dd-'+m+'-total').textContent = c.TOTAL||0;
      if(m!=='ALL'){
        if(E('msc-'+m+'-crit'))    E('msc-'+m+'-crit').textContent    = c.CRITICAL||0;
        if(E('msc-'+m+'-low'))     E('msc-'+m+'-low').textContent     = c.LOW||0;
        if(E('msc-'+m+'-healthy')) E('msc-'+m+'-healthy').textContent = c.HEALTHY||0;
        if(E('msc-'+m+'-over'))    E('msc-'+m+'-over').textContent    = c.OVERSTOCKED||0;
        if(E('msc-'+m+'-total'))   E('msc-'+m+'-total').textContent   = c.TOTAL||0;
      }
    });

    // Fill status cards for selected marketplace
    var counts = d.counts || {};
    E('c-critical').textContent = counts.CRITICAL||0;
    E('c-low').textContent      = counts.LOW||0;
    E('c-healthy').textContent  = counts.HEALTHY||0;
    E('c-over').textContent     = counts.OVERSTOCKED||0;
    E('c-nodata').textContent   = counts['NO DATA']||0;
    E('c-total').textContent    = counts.TOTAL||0;
    var allCard=E('card-ALL-MP');
    if(allCard){
      E('c-all-mp').textContent = '\u221e';
      allCard.classList.toggle('active', activeMarketplace==='ALL');
    }
    E('last-updated').textContent='Updated '+(d.updated_at ? new Date(d.updated_at).toLocaleTimeString() : '--:--');
    cachedHolders = d.holders || [];
    buildHolderTabs(cachedHolders);

    // ── Stale cache check ─────────────────────────────────────────────────────
    // Save server's sync timestamp for future stale checks
    window._lastServerSyncTs = d.updated_at || '';
    // Check dataCache directly — not _stored which is only set at page open
    // _stored stays null after reloadCache() so using it here misses stale checks
    if(dataCache[activeMarketplace] && _isCacheStale(d.updated_at)){
      console.log('Cache stale — server synced at', d.updated_at, '— refreshing silently');
      dataCache[activeMarketplace] = null;
      cacheLoading[activeMarketplace] = false;
      fetchMarketplace(activeMarketplace).then(function(){
        allRows = dataCache[activeMarketplace] || [];
        applyFilters();
        _saveCache(dataCache, d.updated_at); // update sessionStorage with new sync_ts
      });
    }
  }catch(e){ console.error('all-summary error:',e); }
}

// Keep these as no-ops so existing calls still compile
function loadMarketplaceSummary(){ return loadAllSummary(); }
function loadSummary(){ return loadAllSummary(); }

function buildHolderTabs(holders){
  var w=E('holders-wrap');
  var active=activeHolder;

  // If a status filter is active, show only holders that have SKUs with that status
  var visibleHolders = holders;
  if(activeStatus && allRows.length){
    var holdersWithStatus = {};
    var filteredRows = allRows.filter(function(r){ return r.status === activeStatus; });
    filteredRows.forEach(function(r){ holdersWithStatus[r.holder] = (holdersWithStatus[r.holder]||0)+1; });
    visibleHolders = holders.filter(function(h){ return holdersWithStatus[h.name]; });
    // Update counts to reflect filtered status count
    visibleHolders = visibleHolders.map(function(h){
      return {name:h.name, total:holdersWithStatus[h.name]||0, critical:h.critical};
    });
  }

  var html='<button class="hbtn'+(active==='ALL'?' active':'')+'" data-h="ALL" onclick="filterHolder(this)">All Holders</button>';
  visibleHolders.forEach(function(h){
    var crit = activeStatus==='CRITICAL' ? '<span class="bc">'+h.total+'</span>' :
               (h.critical>0 && !activeStatus ? '<span class="bc">'+h.critical+'</span>' : '');
    html+='<button class="hbtn'+(active===h.name?' active':'')+'" data-h="'+esc(h.name)+'" onclick="filterHolder(this)">'+esc(h.name)+crit+'<span class="tc">'+h.total+'</span></button>';
  });
  w.innerHTML=html;
}

// ── Data ───────────────────────────────────────────────────────────────────
function showSkeleton(){
  var skRows = Array(12).fill(0).map(function(){
    return '<tr><td colspan="11"><div class="skeleton-row"></div></td></tr>';
  }).join('');
  E('tb').innerHTML = skRows;
}

// ── Fetch one marketplace into cache ─────────────────────────────────────────
async function fetchMarketplace(mp){
  if(dataCache[mp] !== null || cacheLoading[mp]) return;
  cacheLoading[mp] = true;
  try{
    var r = await (await fetch('/api/stock?marketplace='+encodeURIComponent(mp))).json();
    dataCache[mp] = Array.isArray(r) ? r : [];
    _saveCache(dataCache, window._lastServerSyncTs || ''); // persist with server sync time
  }catch(e){
    dataCache[mp] = [];
    console.error('fetchMarketplace '+mp+' failed:', e);
  }
  cacheLoading[mp] = false;
}

// ── Preload ONLY the active marketplace first — do NOT background-load others ──
// CRITICAL FIX: background-loading all 4 marketplaces sends ~11MB through
// Cloudflare tunnel causing 4-minute waits. Each marketplace = ~2.7MB.
// Now: load only what user needs. Other marketplaces load on first click.
async function preloadAllCaches(){
  await fetchMarketplace(activeMarketplace);
  if(window._skTimer){ clearInterval(window._skTimer); window._skTimer=null; }
  allRows = dataCache[activeMarketplace] || [];
  applyFilters();
  // Do NOT background-load other marketplaces — they load on first click (cache miss)
  // This reduces initial load from ~11MB to ~2.7MB through Cloudflare
}

// ── Wipe cache and reload everything ────────────────────────────────────────
function reloadCache(){
  dataCache = {ALL:null, UK:null, US:null, Germany:null};
  cacheLoading = {ALL:false, UK:false, US:false, Germany:false};
  _stored = null;
  try{
    sessionStorage.removeItem(CACHE_KEY);
    sessionStorage.removeItem(CACHE_TS_KEY);
    sessionStorage.removeItem(CACHE_SYNC_KEY);
  }catch(e){}
  preloadAllCaches();
}

async function loadData(){
  if(dataCache[activeMarketplace] !== null){
    // Cache hit — instant, no skeleton, no API call
    allRows = dataCache[activeMarketplace] || [];
    applyFilters();
    return;
  }
  // Cache miss — show skeleton and fetch
  showSkeleton();
  try{
    await fetchMarketplace(activeMarketplace);
    if(window._skTimer){ clearInterval(window._skTimer); window._skTimer=null; }
    allRows = dataCache[activeMarketplace] || [];
    applyFilters();
  }catch(e){
    if(window._skTimer){ clearInterval(window._skTimer); window._skTimer=null; }
    console.error(e);
    E('tb').innerHTML='<tr><td colspan="11"><div class="loading-wrap" style="padding:40px 24px;">'
      +'<div style="font-size:1.8rem;margin-bottom:4px;">&#9888;</div>'
      +'<div class="loading-text" style="color:var(--red);font-size:0.78rem;margin-bottom:8px;">Failed to load &#8212; server may be restarting</div>'
      +'<div class="loading-text" style="font-size:0.68rem;margin-bottom:12px;">'+e.message+'</div>'
      +'<button onclick="reloadCache()" style="background:var(--blue);border:none;color:#fff;border-radius:6px;padding:7px 18px;font-size:0.75rem;cursor:pointer;font-family:\'DM Sans\',sans-serif;">&#8635; Retry</button>'
      +'</div></td></tr>';
  }
}


// ── Filters — search + status dropdown work on already-loaded marketplace data
function applyFilters(){
  // ── FIX: Do not filter if data is still loading ───────────────────────────
  // cacheLoading[mp]=true means fetch is in progress — rows not ready yet.
  // Showing "No data" while loading causes the glitch seen in screenshots.
  if(cacheLoading[activeMarketplace]){
    showSkeleton(); // keep showing skeleton until data arrives
    return;
  }

  var q=E('qbox').value.trim().toLowerCase();
  var sf=E('sf').value;
  // Always pull from cache for active marketplace (not stale allRows)
  var rows = (dataCache[activeMarketplace] || allRows).slice();

  // ── FIX: If cache is null (not loaded yet) — show skeleton, not "No data" ─
  if(dataCache[activeMarketplace] === null){
    showSkeleton();
    return;
  }

  // Holder filter — client-side
  if(activeHolder && activeHolder !== 'ALL'){
    rows = rows.filter(function(r){ return r.holder === activeHolder; });
  }
  // Search within current marketplace data only
  if(q) rows=rows.filter(function(r){
    return r.sku.toLowerCase().indexOf(q)>=0||
           (r.product_name||'').toLowerCase().indexOf(q)>=0||
           (r.holder||'').toLowerCase().indexOf(q)>=0;
  });
  // Status filter
  if(sf) rows=rows.filter(function(r){return r.status===sf;});
  // Rebuild holder tabs to show only holders relevant to current filter
  if(cachedHolders.length) buildHolderTabs(cachedHolders);

  var mktLabel = activeMarketplace !== 'ALL' ? ' · '+activeMarketplace : '';
  E('rc').textContent = rows.length.toLocaleString()+' rows shown'+mktLabel;
  renderTable(rows);
}

function buildRow(r){
  var nm=r.product_name?esc(r.product_name):'<span style="color:#4b5563;font-size:0.68rem">Not Listed</span>';
  var pl=r.platform?'<span class="'+platClass(r.platform)+'">'+esc(r.platform)+'</span>':'<span style="color:#4b5563">-</span>';
  var dy=r.days_remaining!=null?r.days_remaining:'—';
  var bg=r.status.replace(' ','-');
  var holderColors=['#6366f1','#8b5cf6','#ec4899','#14b8a6','#f59e0b','#10b981','#3b82f6','#ef4444','#a78bfa','#06b6d4'];
  function holderColor(name){var h=0;for(var i=0;i<name.length;i++)h=(h*31+name.charCodeAt(i))&0xffff;return holderColors[h%holderColors.length];}
  var hc=holderColor(r.holder||'');
  var holderHtml='<span class="holder-badge"><span class="h-dot" style="background:'+hc+'"></span>'+esc(r.holder)+'</span>';
  var remapBtn;
  if(activeMarketplace==='ALL'){
    remapBtn='<span style="color:#374151;font-size:0.65rem;font-family:\'DM Mono\',monospace;">select location</span>';
  } else if(r.status==='CRITICAL'){
    var remapLoc=encodeURIComponent(activeMarketplace);
    var remapSku=encodeURIComponent(r.sku);
    remapBtn='<a href="/remap?sku='+remapSku+'&location='+remapLoc+'" '
      +'style="display:inline-flex;align-items:center;gap:4px;background:rgba(239,68,68,.12);'
      +'border:1px solid rgba(239,68,68,.3);color:#fca5a5;border-radius:6px;'
      +'padding:4px 10px;font-size:0.62rem;font-weight:700;text-decoration:none;'
      +'white-space:nowrap;transition:background .15s;" '
      +'onmouseover="this.style.background=\'rgba(239,68,68,.22)\'" '
      +'onmouseout="this.style.background=\'rgba(239,68,68,.12)\'">&#8635; Remap</a>';
  } else {
    remapBtn='<span style="color:#374151;font-size:0.68rem;">\u2014</span>';
  }
  return '<tr>'
    +'<td class="sk">'+esc(r.sku)+'</td>'
    +'<td class="nm" title="'+esc(r.product_name)+'">'+nm+'</td>'
    +'<td>'+pl+'</td>'
    +'<td>'+r.stock.toLocaleString()+'</td>'
    +'<td>'+(r.inbound||'—')+'</td>'
    +'<td>'+(r.reorder_point||'—')+'</td>'
    +'<td>'+r.avg_per_day+'</td>'
    +'<td>'+dy+'</td>'
    +'<td><span class="badge '+bg+'">'+esc(r.status)+'</span></td>'
    +'<td>'+holderHtml+'</td>'
    +'<td>'+remapBtn+'</td>'
    +'</tr>';
}

// Cancel any in-progress background render
var _renderToken = 0;

function renderTable(rows){
  var tb=E('tb');
  // ── FIX: always cancel in-flight background render first ─────────────────
  // incrementing _renderToken before the rows check ensures any previous
  // requestAnimationFrame loop sees a stale token and stops immediately.
  // This prevents old rows appearing below the "No data" message.
  ++_renderToken;

  if(!rows.length){
    tb.innerHTML='<tr><td colspan="11"><div class="loading-wrap">'
      +'<div style="font-size:1.5rem;opacity:.4;">&#128230;</div>'
      +'<div class="loading-text">No data matching filters.</div>'
      +'</div></td></tr>';
    return;
  }

  // ── Stage 1: Paint first 100 rows IMMEDIATELY — user sees data at once ──────
  var FIRST = 100;
  var CHUNK = 200; // rows per background batch
  tb.innerHTML = rows.slice(0, FIRST).map(buildRow).join('');
  tb.classList.remove('fade-in');
  void tb.offsetWidth;
  tb.classList.add('fade-in');

  if(rows.length <= FIRST) return; // small result — done

  // ── Stage 2: Render remaining rows in background chunks ───────────────────
  // Each chunk uses requestAnimationFrame so browser stays responsive
  // Use current _renderToken value (already incremented above the rows check)
  var token = _renderToken; // capture current token — do NOT increment again
  var offset = FIRST;

  function renderChunk(){
    if(token !== _renderToken) return; // filter changed — abort this render
    if(offset >= rows.length) return;  // all done

    var fragment = rows.slice(offset, offset + CHUNK).map(buildRow).join('');
    // insertAdjacentHTML is faster than innerHTML rebuild for appending
    tb.insertAdjacentHTML('beforeend', fragment);
    offset += CHUNK;

    if(offset < rows.length){
      requestAnimationFrame(renderChunk); // next chunk on next frame
    }
  }
  requestAnimationFrame(renderChunk);
}

// ── Status filter (card click or dropdown) ──────────────────────────────────
function onStatusDrop(){
  var v=E('sf').value;
  activeStatus=v;
  document.querySelectorAll('.card').forEach(function(c){c.classList.remove('active-card');});
  if(v){var c=E('card-'+v.replace(' ',''));if(c)c.classList.add('active-card');}
  // Re-filter already loaded rows — no new API call needed
  applyFilters();
}

function filterStatus(s){
  activeStatus=s;
  E('sf').value=s;
  document.querySelectorAll('.card').forEach(function(c){c.classList.remove('active-card');});
  if(s){var c=E('card-'+s.replace(' ',''));if(c)c.classList.add('active-card');}
  // Re-filter already loaded rows — no new API call needed
  applyFilters();
}

function filterHolder(btn){
  activeHolder=btn.dataset.h;
  document.querySelectorAll('.hbtn').forEach(function(b){b.classList.toggle('active',b===btn);});
  // Filter from cache — no API call, instant response
  applyFilters();
}

// ── Chat ───────────────────────────────────────────────────────────────────
function toggleChat(){
  chatOpen=!chatOpen;
  E('chat-panel').classList.toggle('hidden',!chatOpen);
}
function sendSugg(el){E('chat-in').value=el.textContent;sendChat();}

async function sendChat(){
  var inp=E('chat-in');
  var msg=inp.value.trim();
  if(!msg) return;
  inp.value='';
  E('chat-btn').disabled=true;
  addMsg(msg,'user');
  var loadId='load-'+Date.now();
  addMsg('Thinking...','bot loading-msg',loadId);
  try{
    var res=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
    var d=await res.json();
    var reply=d.response||d.error||'No response';
    var el=document.getElementById(loadId);
    if(el) el.remove();
    addMsg(reply,'bot');
  }catch(e){
    var el=document.getElementById(loadId);
    if(el) el.remove();
    addMsg('Error connecting to OpenClaw.','bot');
  }
  E('chat-btn').disabled=false;
  loadMarketplaceSummary(); loadSummary(); reloadCache();
}

function addMsg(text,cls,id){
  var div=document.createElement('div');
  div.className='msg '+cls;
  if(id) div.id=id;
  div.innerHTML=text.replace(/\*\*(.*?)\*\*/g,'<b>$1</b>').replace(/\n/g,'<br>');
  var msgs=E('chat-messages');
  msgs.appendChild(div);
  msgs.scrollTop=msgs.scrollHeight;
}

// ── Countdown ──────────────────────────────────────────────────────────────
function countdown(){
  refreshIn--;
  if(refreshIn<=0){
    refreshIn=1200;
    // Only call loadAllSummary — it checks server synced_at vs cached sync_ts
    // and silently re-fetches only if DB actually has new data.
    // Do NOT call reloadCache() here — that blindly clears cache and
    // re-fetches all data even when nothing changed in the DB.
    // stale detection inside loadAllSummary() handles everything correctly.
    loadAllSummary();
  }
  var m=Math.floor(refreshIn/60), s=refreshIn%60;
  E('nr').textContent='Next refresh in '+(m>0?m+'m ':'')+s+'s';
  setTimeout(countdown,1000);
}

// ── Read URL params on page load (e.g. from remap holder badge click) ─────────
function checkUrlParams(){
  var params = new URLSearchParams(window.location.search);

  // ?holder=Dilani  → pre-select that holder tab
  var holderParam = params.get('holder');
  if(holderParam && holderParam !== 'ALL'){
    activeHolder = holderParam;
    window._pendingHolder = holderParam;
  }

  // ?marketplace=UK → pre-select that marketplace card
  var mpParam = params.get('marketplace');
  if(mpParam && ['UK','US','Germany'].indexOf(mpParam) >= 0){
    activeMarketplace = mpParam;
  }
}

// After loadData renders holder tabs, mark the correct one active
function applyPendingHolder(){
  if(!window._pendingHolder) return;
  var h = window._pendingHolder;
  window._pendingHolder = null;
  var btns = document.querySelectorAll('.hbtn');
  for(var i = 0; i < btns.length; i++){
    if(btns[i].dataset.h === h){
      document.querySelectorAll('.hbtn').forEach(function(b){ b.classList.remove('active'); });
      btns[i].classList.add('active');
      btns[i].scrollIntoView({behavior:'smooth', block:'nearest', inline:'center'});
      break;
    }
  }
}

// ── Initial load — smart cache-aware startup ─────────────────────────────────
// F5 / hard reload → clear sessionStorage → always fetch fresh data
// Navigation (link/back) → use sessionStorage cache if still valid
//
// performance.navigation.type:
//   0 = TYPE_NAVIGATE  (link click, address bar)  → may use cache
//   1 = TYPE_RELOAD    (F5, Ctrl+R)               → clear cache, force fresh
//   2 = TYPE_BACK_FORWARD (browser back/forward)  → use cache
checkUrlParams();

var _navType = (performance && performance.navigation) ? performance.navigation.type : 0;
if(_navType === 1){
  // F5 / reload — clear sessionStorage so fresh data always loads after sync
  _stored = null;
  dataCache = {ALL:null, UK:null, US:null, Germany:null};
  try{
    sessionStorage.removeItem(CACHE_KEY);
    sessionStorage.removeItem(CACHE_TS_KEY);
    sessionStorage.removeItem(CACHE_SYNC_KEY);
  }catch(e){}
}

if(_stored && dataCache[activeMarketplace]){
  // Cache hit from navigation (not F5) — render instantly, check staleness
  allRows = dataCache[activeMarketplace] || [];
  applyFilters();
  loadAllSummary().then(function(){ applyPendingHolder(); });
} else {
  // No cache or F5 reload — sequential: summary first, then data
  // Avoids multiple simultaneous requests through Cloudflare tunnel
  showSkeleton();
  loadAllSummary()
    .then(function(){
      return preloadAllCaches();
    })
    .then(function(){ applyPendingHolder(); })
    .catch(function(){ applyPendingHolder(); });
}

countdown();
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)