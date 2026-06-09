# Skills Portfolio — LEDs One Operations Stock & Performance Monitor (OSPM)

> Evidence-based skills assessment derived **exclusively** from project deliverables D01–D09
> (27 May 2026 – 08 June 2026). Every skill below cites the document(s) that prove it.
> Developer: G. Sarujanan · Requirement REQ-01 · Code: `/opt/openclaw/stock_level/`

**Proficiency scale**
- **Beginner** — basic implementation with guidance.
- **Intermediate** — independent implementation of common patterns.
- **Advanced** — designed architecture, optimization strategies, and business logic; solved complex problems independently.

*Calibration note: a strict curve is applied. "Advanced" is reserved for the skills where the documents show original design, measurable optimization, or independent root-cause resolution. Routine usage is rated Intermediate even where competence is solid.*

---

## 1. Project Summary

**Project Name:** LEDs One Operations Stock & Performance Monitor (OSPM) — Stock Level Dashboard + Remap Automation.

**Project Objective:** Provide a real-time, multi-marketplace (UK / US / Germany) inventory dashboard that classifies every SKU by stock health using a sales-velocity waterfall, and an automated **remap engine** that, when a product goes out of stock, recommends the best in-stock sibling from the same product family — preserving sales continuity without manual lookup.

**System Overview:**
- **Source of truth:** remote MySQL (`centralizer`, `listing_management`, `order_management`) — read only by the sync job (D01).
- **Serving store:** local PostgreSQL `stock_level` database — all web reads (D03, D04).
- **ETL:** `db_sync.py`, MySQL→PostgreSQL across ~10 tables, every 20 minutes via OpenClaw cron (D01–D03, D07).
- **Web layer (Flask):** `dashboard_server.py` (dashboard + proxy routes), `remap_server.py` (remap page/API + product-detail API), `product_detail_card.py` (landscape comparison page); `stock_dashboard_writer.py` exports to Google Sheets (D02–D04).
- **Delivery:** Cloudflare tunnel; vanilla-JS frontend with sessionStorage caching (D06).

**Business Impact (as documented):**
- Corrected SKU count from an inflated **42,505 → ~3,588** by fixing the source table (D01).
- Eliminated duplicate remap suggestions (e.g. ENC5693 appearing 8×) by deduplicating to one row per `mapped_sku` (D02).
- Reduced Cloudflare-tunnel dashboard load from **~4 minutes → ~1.3 s** per marketplace via gzip, pooling, payload reduction and index-covered sorting (D06).
- Surfaced **+2,124** previously-hidden NULL-`mapped_sku` products (3,608 → 5,732 SKUs) through the effective-SKU fallback (D07, D08).
- Eliminated **13 cross-holder remap suggestions** (→ 0) by enforcing a same-holder rule, and corrected wrong-product-type suggestions via a 3-level parts-priority match (D09).

---

## 2. Backend Development

### Python — **Advanced**
- **Evidence:** D02, D03, D08, D09. Core language for the entire ETL and server stack.
- **Examples:** dictionary-based deduplication (`best[mapped_sku]`, D02); regex multipack exclusion `re.search(r'\d+PK', sku.upper())` (D09); JSON parsing of `selected_variations` for colour extraction (D02); score-tuple ranking `(site_rank, -same_parts_siblings, has_color)` (D09).

### Flask — **Advanced**
- **Evidence:** D02, D03, D06. Multi-module Flask app with on-demand cross-module imports (`dashboard_server.py` imports `remap_server`, `product_detail_card`).
- **Examples:** custom `Response` with `Content-Encoding: gzip` headers instead of `jsonify()` (D06); thin proxy routes (`/remap`, `/api/remap-suggestions`, `/product-detail-card`) keeping modules decoupled (D02, D03).

### REST API Development — **Advanced**
- **Evidence:** D04, D06, D08. Designed endpoints around payload and round-trip cost.
- **Examples:** combined `/api/all-summary` to replace two calls (D06); lightweight `/api/remap-summary` single `GROUP BY` replacing three 7-CTE calls (D06); real `synced_at` returned from every `updated_at`-bearing endpoint (D07, D08).

### Connection Pooling — **Advanced**
- **Evidence:** D06. `ThreadedConnectionPool(2, 10)` replacing per-request `psycopg2.connect()`, with the invariant that `pg_release(conn)` is always called in a `finally` block; documented cold-start (`min=2`) and exhaustion (`max=10`) trade-offs.

### Data Synchronization / ETL — **Advanced**
- **Evidence:** D01, D03, D07, D08. Owns the full MySQL→PostgreSQL pipeline across ~10 tables.
- **Examples:** added `sync_amazon_variants`, `sync_bullet_points`, `sync_sub_images`, `sync_ebay_product_titles` (D02, D03, D05); resequenced sync so `dashboard_cache` lands at ~66 s instead of 156 s (D07).

### Error Handling & Validation — **Intermediate**
- **Evidence:** D02, D04. Null guards on `best_sibling` joins, `IS NOT NULL` guard to avoid NULL-`parent_sku` cross-matches (D04); NO DATA velocity-waterfall fallback (D02); JS loading guards preventing "No data" flashes (D07). Solid defensive practice, but largely conventional patterns rather than a designed validation framework.

---

## 3. Frontend Development

### JavaScript (vanilla) — **Advanced**
- **Evidence:** D06, D07. No framework; hand-built caching and render control.
- **Examples:** server-`synced_at` stale detection (D07); `++_renderToken` to cancel in-flight background renders (D06/D07); F5-vs-navigation differentiation via `performance.navigation.type` (D07).

### Session Storage & State Persistence — **Advanced**
- **Evidence:** D06. Cross-page cache (`ledsone_dash_cache`, `ledsone_remap_cache`/`_state`) with 20-min TTL aligned to the sync cycle; `_saveRemapState()` before navigation and restore-on-init (instant render, zero API calls on back-navigation).

### Progressive Rendering — **Advanced**
- **Evidence:** D06, D07. 100-row immediate paint + remaining rows in 200-row `requestAnimationFrame` chunks; render-token cancellation on filter change to avoid freezing on ~3,600-row tables.

### Navigation State Management — **Advanced**
- **Evidence:** D05, D06. URL-param preselection (`?holder=`, `?marketplace=`) wired into init before `loadData()`, with `applyPendingHolder()` invoked on both `.then()` and `.catch()`.

### HTML / CSS — **Intermediate**
- **Evidence:** D04, D05, D07, D09. Sticky first/last table columns with edge-fade shadow (D05); `attr(title)` hover tooltips (D09); metric cards with accent bars (D09); thin styled scrollbars (D07); CSS-grid column alignment fixes (D04). Capable, focused styling work rather than a broad design system.

---

## 4. Database & Data Engineering

### SQL Query Optimization — **Advanced** ⭐
- **Evidence:** D06, D08. Two flagship optimizations.
- **Examples:** **Two-query split pattern** — replacing `COALESCE`/`CASE WHEN` on indexed columns with separate `mapped_sku IS NOT NULL` / `IS NULL` queries combined in Python, restoring MySQL index use on 33k–45k-row tables (D08); **index-covered sort** — precomputed `sort_order SMALLINT` + composite index `(location, sort_order, sku)` eliminating the in-memory sort of ~3,600 rows (D06).

### PostgreSQL Schema Design — **Advanced**
- **Evidence:** D05, D06. Master-vs-display **two-table pattern** (`ebay_products` for counting, `ebay_product_titles` for localized display via `LEFT JOIN` + `COALESCE`) so localisation never drops counts (D05); schema extension from 4→12 columns plus `product_bullet_points` / `product_sub_images` (D03); `sort_order` column added for index-friendly ordering (D06).

### Multi-CTE Query Design — **Advanced** ⭐
- **Evidence:** D02, D04, D09. 7-CTE remap query (`loc_stock → av_deduped → oos → siblings → best_sibling` …) (D02); upgraded to 3 priority CTEs (P1 same base_sku+parts, P2 same parts, P3 any) merged by `COALESCE` for parts-aware sibling selection (D09).

### Window Functions — **Advanced**
- **Evidence:** D02, D05. `ROW_NUMBER() OVER (PARTITION BY TRIM(mapped_sku), site …)` for best-title selection; `DISTINCT ON` to guarantee one output row per OOS SKU / per `mapped_sku`.

### Data Deduplication Strategies — **Advanced** ⭐
- **Evidence:** D02, D03, D09. Defence-in-depth dedup at both sync (Python dict) and query (`DISTINCT ON`) levels (D02); `(mapped_sku, image_url)` dedup for sub-images (D03); score-tuple dedup choosing the correct family/site row (D09).

### MySQL Integration — **Advanced**
- **Evidence:** D01, D02, D05. Identified correct source tables (`centralizer.location_wise_inv_stock` over `inv_final_stock`), correct columns (`oii_item_sku`), the `id = bullet_points.product_id` link (D03), `site` vs `region` distinction for localisation (D05), and CHAR-padding handled via `TRIM()` (D02).

### Data Integrity Validation — **Advanced**
- **Evidence:** D05, D08. Count-integrity invariant ("count always comes from the master table; no SKU excluded by a missing localized title", D05); full line-by-line post-change verification tables across all four files (D08).

### Composite Index Design — **Advanced**
- **Evidence:** D06. `idx_dc_loc_sort(location, sort_order, sku)` chosen specifically to cover filter + sort in one index-only scan.

---

## 5. System Design & Architecture

### Remap Engine Design — **Advanced** ⭐
- **Evidence:** D02, D04, D09. The project's core algorithm: parent-SKU family grouping → sibling selection → single best replacement (D02); corrected from `current_stock===0` to `dashboard_status`-based sectioning and INNER→LEFT JOIN so all CRITICAL SKUs appear (D04); same-holder hard rule + parts-priority matching (D09).

### Parent-SKU Selection Logic — **Advanced** ⭐
- **Evidence:** D09. Deterministic family selection via score tuple `(site_rank, -same_parts_single_unit_count, has_color, has_parent)` with UK-first site priority, multipack exclusion from family-size counting, and non-NULL-parent preference.

### Variant Matching Algorithm — **Advanced**
- **Evidence:** D09. Parts-count product taxonomy (parts=1 base, =2 +accessory, =3 +bulb) with P1/P2/P3 waterfall ensuring like-for-like product structure over raw stock.

### Inventory / Dashboard System Design — **Advanced**
- **Evidence:** D01–D03. End-to-end design: MySQL source → PostgreSQL serving store → Flask web → Google Sheets export, with clear file responsibilities and "all web reads from PostgreSQL" rule (D03).

### Cache Invalidation Strategy — **Advanced**
- **Evidence:** D06, D07. Server-`synced_at` staleness comparison (not client clock); F5 clears sessionStorage while navigation restores it; TTL retained only as a safety fallback (D07).

### Multi-Source Data Integration — **Advanced**
- **Evidence:** D01, D02. Unifies stock (centralizer), listings (listing_management), and sales velocity (order_management) on the `mapped_sku` / `effective_sku` key.

---

## 6. Domain Skills

### Inventory Optimization & Out-of-Stock Prevention — **Advanced** ⭐
- **Evidence:** D01, D02. Velocity waterfall (7/14/30-day windows, 7-unit minimum sample) → status tiers (CRITICAL ≤7d, LOW ≤21d, HEALTHY ≤90d, OVERSTOCKED, NO DATA); OOS threshold stock ≤5; alternative threshold stock >10.

### SKU Remapping Strategy — **Advanced**
- **Evidence:** D02, D04, D09. Full remapping policy including same-holder portfolio boundary, parts-aware matching, and location-specific stock checks.

### Amazon / Multi-Marketplace Operations — **Advanced**
- **Evidence:** D02, D05. ASIN/`parent_sku` variant families; per-site (UK/US/Germany) listings and title localisation; `'Germany'` vs `'DE'` data caveat.

### Holder-Based Portfolio Management — **Advanced**
- **Evidence:** D04, D09. `ph_mapping` holder assignment; same-holder remap enforced as a hard SQL JOIN, not a soft filter (D09 DECISION-01).

### Variant-Based Product Mapping — **Intermediate**
- **Evidence:** D09. Colour/size variant grouping under parent_sku; rated Intermediate because variant *colour* data is documented as unreliable and used informationally only (D02 GAP-03).

---

## 7. Performance Engineering

### Cloudflare Tunnel Optimization — **Advanced** ⭐
- **Evidence:** D06. Diagnosed six root causes of a 4-minute load; load only the active marketplace (no background preload of all four); ≤2 parallel API calls; documented ~1.5 Mbps tunnel-throughput constraint as a reusable rule. **Result: ~4 min → ~1.3 s.**

### Gzip Compression — **Advanced**
- **Evidence:** D06. `gzip_json()` at compresslevel 6 (deliberately over 9 for CPU/size balance); **2.7 MB → ~240–350 KB, ~87% reduction**, plus removal of 4 unused fields (~30% further payload cut).

### Sync Performance Tuning — **Advanced**
- **Evidence:** D07, D08. Two-query split removed full-table scans on title/variant syncs; sync resequencing made the dashboard fresh at ~66 s vs 156 s; correctly attributed a 389 s spike to MySQL server load, not a code regression (D08).

### Connection Pooling & Index-Driven Sort — **Advanced**
- **Evidence:** D06. (See §2 and §4.) Pooling removed per-request TCP/SSL/auth overhead; composite index removed in-memory sort.

### Session Caching — **Advanced**
- **Evidence:** D06. sessionStorage cross-page cache cut full re-fetches on navigation to a single counts refresh.

---

## 8. Problem Solving

Each entry follows: **problem → root cause → fix → validation.**

1. **SKU count inflated to 42,505 (D01).** Root cause: read from `inv_final_stock` (297,499 rows, per-warehouse). Fix: switch to `location_wise_inv_stock` with `SUM … GROUP BY`. Validated: count returned to ~3,588 matching the leader dashboard.
2. **ENC5693 appeared 8× on remap (D02).** Root cause: `amazon_variants` stored one row per ASIN. Fix: dedup to one row per `mapped_sku` at sync **and** `DISTINCT ON` in query (defence in depth). Validated: US remap rows fell from 13,287 to expected range.
3. **CRITICAL SKUs missing from remap (D04).** Root cause: INNER JOIN on `amazon_variants` excluded SKUs without variants. Fix: LEFT JOIN + null guard; section split by `dashboard_status` not `current_stock===0`. Validated: such SKUs now show as NO_ALTERNATIVE.
4. **Title localisation dropped Germany count to ~1,200 (D05).** Root cause: `AND site = %s` filter excluded SKUs lacking a site row. Fix: master + display two-table pattern with `COALESCE`. Validated: counts preserved, German titles shown when present.
5. **Cloudflare 4-minute load (D06).** Root cause: 11 MB uncompressed multi-marketplace preload over a ~1.5 Mbps tunnel + new TCP per request + CASE-in-ORDER-BY. Fix: gzip, pooling, active-only load, composite index, combined endpoints. Validated: ~1.3 s per marketplace.
6. **Stale detection never fired (D07).** Root cause: APIs returned `datetime.utcnow()`; the `_stored` flag also went null after first reload. Fix: return real `MAX(synced_at)`; check `dataCache[...]` directly. Validated across all four `updated_at` endpoints (D08).
7. **+2,124 products invisible (D07/D08).** Root cause: NULL `mapped_sku` rows filtered out. Fix: `effective_sku = COALESCE(NULLIF(TRIM(mapped_sku),''), TRIM(sku))` applied across sync functions. Validated: 3,608 → 5,732 SKUs.
8. **Wrong-holder & wrong-type remap suggestions (D09).** Root cause: siblings could be any holder's product and any parts count. Fix: same-holder SQL JOIN + P1/P2/P3 parts-priority CTEs. Validated: 13 cross-holder cases → 0; documented before/after tables per SKU.

---

## 9. Soft Skills Demonstrated

- **Analytical thinking — Advanced.** Quantified every problem (row counts, byte sizes, timings) before fixing; e.g. NO DATA breakdown (310 zero-stock vs 2,596 has-stock, D02).
- **Debugging & troubleshooting — Advanced.** Isolated subtle bugs: render-token placement, `_stored` lifecycle, CHAR trailing-space padding (D07, D02).
- **Documentation — Advanced.** The D01–D09 records themselves meet a self-defined "3 AM standard" — structured metadata, GAP/FAILURE/DECISION sections, reusable "company knowledge" extracts.
- **Systematic problem solving — Advanced.** Consistent root-cause → fix → validation → edge-case discipline in every deliverable.
- **Technical decision-making — Advanced.** Reasoned trade-offs recorded as explicit DECISIONs (count-integrity over filtering D05; gzip level 6 over 9 D06; defence-in-depth dedup D02).
- **Requirement analysis — Intermediate.** Strong at deriving rules from observed data and leader's reference code; less evidence of independent up-front requirements authoring.

---

## 10. Final Assessment

### Top 10 Strongest Skills Demonstrated
1. **Remap Engine Design** (D02, D04, D09) — the system's core algorithm, evolved across three deliverables.
2. **SQL Query Optimization** (D06, D08) — two-query split + index-covered sort, with measurable wins.
3. **Parent-SKU Selection Logic** (D09) — deterministic multi-factor scoring tuple.
4. **Cloudflare / Performance Engineering** (D06) — 4 min → 1.3 s, six root causes resolved.
5. **Data Deduplication Strategies** (D02, D03, D09) — defence-in-depth across sync and query layers.
6. **PostgreSQL Schema Design** (D05, D06) — master/display pattern + index-oriented columns.
7. **Multi-CTE Query Design** (D02, D09) — 7-CTE pipeline + parts-priority COALESCE.
8. **Data Synchronization / ETL** (D01, D03, D07, D08) — ~10-table pipeline, resequenced for freshness.
9. **Inventory Optimization & OOS Prevention** (D01, D02) — velocity waterfall and status tiering.
10. **Root-Cause Problem Solving** (D01, D02, D06) — repeatable quantify→fix→validate method.

### Overall Developer Proficiency Assessment
A strong **full-stack data engineer / backend-leaning developer** operating at an **Advanced** level within this domain. The standout capability is **database and performance engineering applied to a real business problem** — turning vague inventory pain into deterministic, index-efficient SQL and a defensible remap algorithm, then validating each change against production data. Frontend work is competent and pragmatic (vanilla JS, sessionStorage, progressive rendering) rather than design-system-deep. The documentation discipline is exceptional and itself a marketable skill. Demonstrated ability to work independently end-to-end: source-system reverse-engineering, ETL, API, frontend, deployment (Cloudflare/systemd/cron) and operational verification.

### Suggested Improvements for Future Projects
1. **Automated testing.** No unit/integration test evidence; the manual "post-change verification" tables (D08) would be far stronger as a regression suite. Add `pytest` coverage for the velocity waterfall, dedup, and remap scoring.
2. **Externalize hardcoded thresholds.** CRITICAL/LOW/OVERSTOCKED days, OOS=5, MIN_ALT_STOCK=10, site priorities are duplicated across Python and SQL (D04 GAP, D09). Move to the proposed BLOS config to remove drift risk.
3. **Close open architectural gaps.** Implement location-specific `parent_sku` (D09 GAP-01) so US/Germany remap stops borrowing UK families; restore scroll position on back-nav (D06/D09 GAP-04).
4. **Connection management consistency.** `remap_server.py` uses direct connections (no pool) and an extra round-trip for `synced_at` (D08 FAILURE-03/GAP-05) — unify on the pool and fold the timestamp into the main query.
5. **Data-quality feedback loop.** MySQL source issues (wrong `parent_sku`, `Germany`/`DE`, 89k unmapped Amazon rows) are mitigated in code but not surfaced; add an automated data-quality report for the operations team.
6. **Observability.** Add structured sync metrics/alerting so anomalies like the 389 s spike (D08) are detected automatically rather than by manual log inspection.

---

*Generated from project deliverables D01–D09. No skill is listed without a citing document; skills lacking evidence (testing frameworks, CI/CD, containerization, ORM) were deliberately excluded.*
