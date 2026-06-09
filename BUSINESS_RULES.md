# BUSINESS RULES — LEDs One Stock & Remap (OSPM)

> **Single authoritative source for every business threshold and rule.**
> Created to prevent threshold drift: several values are currently duplicated across
> Python, SQL, and YAML. Before changing any value, read the **Governance Rule** at the
> bottom and update **every** listed location.
>
> Every rule below is extracted from the implementation and the D01–D09 evidence records —
> nothing here is invented. Code locations are cited so the value can be verified.

---

## Dashboard Status Rules

Computed in `dashboard_server.py` → `compute_status()` (constants at lines 101–103,
logic ~line 122). Values come from `config/stock_dashboard.yaml` with code defaults.

| Status | Condition (days remaining) | Source |
|---|---|---|
| **CRITICAL** | `days <= 7` (`CRIT`) | dashboard_server.py:101,122 |
| **LOW** | `8 ≤ days ≤ 21` (`LOW`) | dashboard_server.py:102 |
| **HEALTHY** | `22 ≤ days ≤ 90` (`OVER`) | dashboard_server.py:103 |
| **OVERSTOCKED** | `days > 90` | dashboard_server.py:125 |
| **NO DATA** | `avg_per_day == 0` (no qualifying velocity) | dashboard_server.py / D01 §5 |

**Days remaining** = `stock / avg_per_day`.

**Velocity waterfall** (minimum 7 units before a window is trusted — D01/D02):
```
if sold_7d  >= 7:  avg = sold_7d  / 7
elif sold_14d >= 7: avg = sold_14d / 14
elif sold_30d >= 7: avg = sold_30d / 30
else:               avg = 0   → NO DATA
```
The 7-unit minimum prevents a single sale from distorting velocity (e.g. 1 sale → false OVERSTOCKED).

---

## Out-of-Stock Rules

| Rule | Value | Source |
|---|---|---|
| Out-of-stock threshold | `stock <= 5` | remap_server.py:86 (`OUT_OF_STOCK_THRESHOLD = 5`) |

A SKU at stock ≤ 5 with no sales history (NO DATA) is treated as an out-of-stock risk.

---

## Remap Eligibility Rules

Enforced in `remap_server.py` `REMAP_QUERY` (OOS CTE ~line 188–223) and on the dashboard
remap button.

**Eligible for remap:**
- `dashboard_status = CRITICAL` (days ≤ 7, including zero stock with sales).
- `dashboard_status = NO DATA` **and** `stock <= 5`.

**NOT eligible:** LOW, HEALTHY, OVERSTOCKED, and NO DATA with stock > 5.
(LOW was explicitly excluded — business decision D04/D05: remap is a CRITICAL-only response.)

---

## Alternative SKU Rules

| Rule | Value | Source |
|---|---|---|
| Minimum alternative stock | `stock > 10` | remap_server.py:87 (`MIN_ALT_STOCK = 10`) |

A sibling must have more than 10 units in the **same location** to be suggested.

---

## Holder Rules

**Same-holder remap only** (D09 — hard rule, enforced as a SQL JOIN, not a soft filter):
- The suggested SKU **must** belong to the same portfolio holder as the OOS SKU.
- Cross-holder remap is **prohibited** — each holder manages only their own portfolio.
- If no same-holder sibling with stock > 10 exists → `NO_ALTERNATIVE` (do not fall back
  to another holder's product).

Source: remap_server.py siblings CTE (same-holder JOIN); D09 §2 Change 1, §8.

---

## Variant Matching Rules (Parts-Count Priority)

`parts = number of "+" segments in the SKU`; `base_sku = first segment`.
Suggestion is chosen by COALESCE(P1 → P2 → P3) — D09 §2 Change 2.

| Priority | Rule | Meaning |
|---|---|---|
| **P1** | Same `base_sku` **and** same parts count | Same product, different colour — best match |
| **P2** | Different `base_sku`, same parts count | Same bundle structure, different product |
| **P3** | Any sibling with `stock > 10` | Last-resort fallback |

If P1 resolves to the OOS SKU itself, it is excluded and selection falls to P2 (D09 FAILURE-01).
Example: `PLTEBC+WCWYBM` (parts=2) → `PLTEBS+WCWYBB` (parts=2), **not** a parts=3 combo.

---

## Parent SKU Selection Rules

When a SKU appears in multiple Amazon families, the family is chosen by a score tuple in
`db_sync.py` `sync_amazon_variants()` (SITE_PRIORITY at line 635). **Lower score wins.**

```
score = (site_rank, -same_parts_single_unit_count, has_color)   # + has_parent in D09
```

1. **Site rank** — `{UK: 0, US: 1, Germany: 2, Ireland: 9, other: 5}` (db_sync.py:635,638).
   UK always beats Ireland — prevents Ireland listings contaminating UK groupings.
2. **Family size** — family with the most same-parts single-unit siblings wins.
3. **Multi-pack exclusion** — SKUs matching `\d+PK` (2PK, 3PK, 5PK …) are excluded from the
   family-size count (db_sync.py:641–656), so bundle families don't win unfairly.
   *Note:* multi-packs are excluded from **family-size scoring only** — a multi-pack can
   still be suggested as a sibling if it is the best same-parts option (D09 DECISION-03).
4. **Has color** — tiebreaker (row with colour data wins).
5. **Non-NULL parent preferred** — `has_parent` favours rows that actually have a parent_sku (D09 Change 8).

---

## Product Title Rules

Localization with fallback (D05 — master + display two-table pattern):
```
display_title = ebay_product_titles.title   (site-specific: UK / US / Germany)
              = ebay_products.title          (global best title, if no site title)
```
- For marketplace = ALL → global title only (no site filter).
- **Count integrity invariant:** counts always come from the master `ebay_products`;
  a missing site-specific title degrades display only and **never** drops a SKU from counts.
- Site values are matched exactly as `'UK' / 'Germany' / 'US'` (not ISO `'DE'`) — D05 GAP-02.

---

## PostgreSQL / Data-Source Rules

- **PostgreSQL `stock_level` is the serving layer** — all web reads (dashboard, remap,
  product detail) come from PostgreSQL only (D03 DECISION-01).
- **Direct MySQL reads are allowed only in `db_sync.py`** sync functions. The web layer
  must never import a MySQL driver or connect to MySQL.
- **effective_sku** = `mapped_sku` if present, else original `sku` (fallback). Implemented
  as two indexed queries, not COALESCE, to preserve MySQL index use (D07/D08).

---

## Cache Rules

- **sessionStorage TTL = 25 minutes** (hard expiry) on both dashboard and remap
  (dashboard_server.py:886, remap_server.py:1161). Aligned to the 20-min sync cycle as a
  safety margin.
- **Stale detection uses real `synced_at`** — APIs return `MAX(synced_at)` from
  `dashboard_cache`, never `datetime.utcnow()`. The browser compares server sync time vs
  its cached sync time; if the server is newer, it silently re-fetches (D07/D08).
- **F5 / reload** clears sessionStorage (forces fresh fetch); link/back navigation restores it.

---

## Governance Rule

**Thresholds are currently duplicated. If any business threshold changes, update ALL of:**

| Threshold | Locations to update |
|---|---|
| Days CRITICAL / LOW / OVERSTOCKED (7 / 21 / 90) | `config/stock_dashboard.yaml`; `dashboard_server.py` (`CRIT/LOW/OVER`, `compute_status`); `remap_server.py` `REMAP_QUERY` SQL (CASE thresholds ~lines 146–165) |
| `OUT_OF_STOCK_THRESHOLD = 5` | `remap_server.py:86` (and any dashboard remap-eligibility check) |
| `MIN_ALT_STOCK = 10` | `remap_server.py:87` and the siblings CTE |
| `SITE_PRIORITY` | `db_sync.py:635` |
| sessionStorage TTL (25 min) | `dashboard_server.py:886`, `remap_server.py:1161` |

> **Known governance gap (D04, unresolved):** day thresholds live in both Python and SQL.
> A future improvement is to source them from `stock_dashboard.yaml` in one place and have
> both the dashboard and the remap query read from it. Until then, this table is the
> mandatory checklist for any threshold change.
