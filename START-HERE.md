# START HERE — LEDs One Stock & Remap (OSPM)

> Entry point for a new developer or LLM. Read this first; it tells you what the
> project is, where everything lives, and the order to read things in.
> **Single source of truth for architecture/deployment is [README.md](README.md)** —
> this file does not repeat it, it points to it.

---

## Project Overview

**What it does.** A real-time inventory dashboard + automated remap engine for a
multi-marketplace Amazon/eBay seller (UK / US / Germany). It classifies every SKU
by stock health (CRITICAL / LOW / HEALTHY / OVERSTOCKED / NO DATA) from a sales-velocity
waterfall, and — when a SKU is out of stock — suggests the best in-stock sibling
product from the same product family so the holder can keep selling.

**Why it exists.** Holders were manually checking stock and guessing replacement
products. The system automates both: surfacing what is running out, and proposing a
correct, same-owner, same-product-type replacement.

**Main business objective.** Prevent lost sales from out-of-stock listings by giving
each portfolio holder an accurate, deterministic remap suggestion per marketplace.

---

## Recommended Reading Order

1. **[README.md](README.md)** — architecture, components, DB schema, sync, deployment, API.
2. **START-HERE.md** (this file) — orientation, reading order, concepts.
3. **[BUSINESS_RULES.md](BUSINESS_RULES.md)** — the single authoritative rule/threshold table.
4. **[HANDOVER.md](HANDOVER.md)** — current status, known gaps, risks, reviewer ownership.
5. **`docs/evidence/D01–D09`** — day-wise decision records (the "why" behind each change).
6. **[skills.md](skills.md)** — evidence-traced skills portfolio (optional context).

---

## Repository Structure Overview

```text
/opt/openclaw/stock_level/
├── scripts/
│   ├── dashboard_server.py     Flask app: stock dashboard UI + /api/* + proxy routes
│   ├── remap_server.py         Remap engine: REMAP_QUERY, remap API, product-detail API, remap page
│   ├── product_detail_card.py  Standalone landscape product-comparison page
│   └── db_sync.py              MySQL → PostgreSQL ETL, runs every 20 min via OpenClaw cron
├── config/                     stock_dashboard.yaml (secrets, gitignored) + .example template
├── logs/                       db_sync.log, stock_dashboard.log (gitignored)
├── README.md                   Architecture + deployment — SINGLE SOURCE OF TRUTH
├── skills.md                   Skills portfolio
├── START-HERE.md               This file
├── BUSINESS_RULES.md           Authoritative business rules
├── HANDOVER.md                 Handover + gaps + ownership
└── docs/evidence/              Day-wise deliverable records D01–D09
```

(`scripts/*_check*.py` are dated developer backups, gitignored — ignore them.
`scripts/stock_dashboard_writer.py` is the optional PostgreSQL→Google Sheets export.)

---

## Component Responsibilities

- **Dashboard (`dashboard_server.py`, `/`)** — serves the stock table and summary counts
  from PostgreSQL `dashboard_cache`. Owns the PG connection pool, gzip responses, status
  classification (`compute_status`), and proxy routes to remap/detail. All web reads are
  PostgreSQL-only.
- **Remap engine (`remap_server.py`, `/remap`, `/api/remap-suggestions`)** — runs the
  multi-CTE `REMAP_QUERY` to find OOS SKUs and their best same-holder sibling, returns the
  remap page + product-detail API.
- **Product detail (`product_detail_card.py`, `/product-detail-card`)** — full landscape
  comparison of OOS SKU vs suggested replacement (images, bullets, stock-by-location, price).
- **Sync (`db_sync.py`)** — the only component allowed to read MySQL. Populates ~10
  PostgreSQL tables every 20 min; sequenced so the dashboard is fresh at ~66s.

---

## Key Business Concepts

- **OOS product** — a SKU that is CRITICAL (days remaining ≤ 7, incl. zero stock with sales)
  or NO DATA with stock ≤ 5. These are the products needing a remap.
- **Remap suggestion** — the best in-stock alternative SKU proposed for an OOS product,
  chosen by parts-count priority and constrained to the same holder.
- **Holder ownership** — every managed SKU belongs to one portfolio holder (`ph_mapping`).
  A holder may only be sent replacements from their own portfolio.
- **Parent SKU family** — Amazon `parent_sku` groups colour/size variants of one product.
  Siblings are other SKUs under the same `parent_sku`.
- **Product variants / parts count** — a SKU's structure is read from its `+` segments:
  parts=1 base product, parts=2 base+accessory, parts=3 base+accessory+bulb. Remap matches
  like-for-like parts count first.

See [BUSINESS_RULES.md](BUSINESS_RULES.md) for the exact thresholds and priorities.

---

## Evidence Location

Day-wise deliverable records (the decision/reasoning trail) live in **`docs/evidence/`**:

```
docs/evidence/2026-05-27__sarujanan__ospm__REQ-01-D01.md   ... through ...
docs/evidence/2026-06-08__sarujanan__ospm__REQ-01-D09.md
```

Each record follows a fixed structure: System State → What Changed → DB Findings →
Gaps → Validation Rules → Failure Modes → Decisions → Company Knowledge → LLM Check.
(See [HANDOVER.md](HANDOVER.md) → Evidence Inventory for the full list. If `docs/evidence/`
is not yet populated in your checkout, committing these records is the top open task.)

---

## Important Files to Understand First

1. `scripts/db_sync.py` — `compute`/sync functions; understand the data before the UI.
2. `scripts/dashboard_server.py` — `compute_status()` (the status waterfall) and the pool.
3. `scripts/remap_server.py` — `REMAP_QUERY` (the CTE pipeline) and `remap_suggestions()`.
4. `BUSINESS_RULES.md` — so you don't accidentally change a threshold in one place only.

---

## New Developer Checklist

- [ ] Read README.md (architecture, DB schema, sync table, API endpoints).
- [ ] Read BUSINESS_RULES.md; locate each threshold in the source.
- [ ] Read HANDOVER.md (status, gaps, risks, who reviews what).
- [ ] Skim `docs/evidence/` D01–D09 in date order for the "why".
- [ ] Run a manual sync and confirm `dashboard_cache` populates (README → First Run).
- [ ] Load `/`, `/remap`, and a `/product-detail-card` page locally.
- [ ] Trace one OOS SKU end-to-end: MySQL → db_sync → dashboard_cache → remap suggestion.
- [ ] Note the threshold-duplication governance rule before editing any threshold.
