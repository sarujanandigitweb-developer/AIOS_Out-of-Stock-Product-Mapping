# HANDOVER — LEDs One Stock & Remap (OSPM)

> For the next developer. Read [START-HERE.md](START-HERE.md) and
> [BUSINESS_RULES.md](BUSINESS_RULES.md) first; this file covers status, open gaps,
> risks, ownership, and what to do next.

---

## Current Project Status

- **Implementation: ~98% complete.** All nine deliverables (D01–D09) are done and the
  app is deployed (Flask on port 8080 via Cloudflare tunnel; sync via OpenClaw cron).
- **Completed phases:** D01 → D09.
- **Open items** are deferred-by-design gaps, source-data quality issues, and
  organization/evidence tasks — none block operation.

---

## Major Features Completed

| Day | Feature |
|---|---|
| D01 | Stabilisation — correct source tables (SKU count 42,505 → ~3,588), MySQL holder mapping, cron/systemd migration |
| D02 | Remap engine v1 — `amazon_variants`, multi-CTE remap query, dedup to one row per `mapped_sku` |
| D03 | Product detail card — extended `ebay_products` (12 cols), bullet points + sub-images, PostgreSQL-only reads |
| D04 | Remap correctness — status-based sectioning, INNER→LEFT JOIN (no CRITICAL SKU dropped), LOW excluded |
| D05 | Title localisation (master + display two-table), sticky columns, holder URL pre-selection |
| D06 | Performance — Cloudflare 4 min → ~1.3 s: gzip, connection pool, composite index, sessionStorage, progressive render |
| D07 | Reliability — real `synced_at` stale detection, F5 fresh fetch, sync resequencing, NULL-`mapped_sku` investigation |
| D08 | Data integrity — NULL-`mapped_sku` fallback complete, two-query split (titles + variants), full file verification |
| D09 | Remap logic — same-holder rule, parts-count priority (P1/P2/P3), parent-SKU scoring, multi-pack exclusion, UI polish, README |

---

## Remaining Known Gaps

Extracted from D01–D09 evidence records.

### High Priority
- **ph_mapping last-write-wins (D08 GAP-03).** `ph_mapping` uses `mapped_sku` as PRIMARY
  KEY; if the same SKU maps to multiple holders, `ON CONFLICT DO UPDATE` silently overwrites
  → possible **wrong holder assignment**, which directly affects same-holder remap correctness.
- **MySQL `parent_sku` data quality (D09 GAP-02).** Some products are grouped into the wrong
  family in the MySQL source (different shapes share a `parent_sku`; Ireland vs UK differ).
  Mitigated by the P1 same-base_sku match, but the source data is still wrong.

### Medium Priority
- **`ebay_products` sync still slow / variable (D08 GAP-01, 80–165 s).** Window function on
  45k+ NULL-`mapped_sku` rows has no index; consider a composite index or pre-filter subquery.
- **Location-specific `parent_sku` not implemented (D09 GAP-01).** One global `parent_sku` per
  SKU (UK wins); US/Germany remap may borrow UK families. Requires a `location` column on
  `amazon_variants` — see Deferred Improvements.

### Low Priority
- **89,124 unmapped Amazon rows (D02 GAP-02).** No `mapped_sku` in MySQL → cannot participate
  in remap. Operations data-entry task, not code.
- **`remap_suggestions()` extra DB round-trip (D08 GAP-05).** Opens a second connection for
  `MAX(synced_at)`; could fold into the main query. Minor (remap is low-frequency).
- **`'Germany'` vs `'DE'` site values (D05 GAP-02).** Sync matches `'Germany'` exactly; if the
  source switches to ISO codes, German title sync silently fails (counts unaffected).
- **Parts P3 fallback (D09 GAP-03).** Products with no same-type sibling fall to any sibling.
  Documented as **correct** given the data — not a bug.

---

## Deferred Improvements (intentionally postponed)

- **Location-specific `parent_sku`** — schema change to `amazon_variants` + re-sync
  (D09 DECISION-04). Accepted that US/Germany may use UK `parent_sku` for now.
- **Source data-quality fixes** — wrong `parent_sku` groupings and unmapped rows are
  upstream MySQL issues; code mitigates but cannot fully fix them.
- **Threshold externalisation** — move day thresholds out of duplicated Python/SQL into
  `stock_dashboard.yaml` as the single source (D04 governance gap).
- **Scroll restoration on back-navigation (D06/D09 GAP-04)** — **RESOLVED this session**
  (saved `#content.scrollTop` in `_saveRemapState`, restored after render in `remap_server.py`).
  *Status: implemented in the working tree; commit pending.* No longer a deferred item.

---

## Known Technical Risks

- **Threshold drift** — day thresholds duplicated in Python and SQL; changing one place
  silently diverges the dashboard from the remap query. Mitigation: BUSINESS_RULES.md
  Governance table is the mandatory change checklist.
- **Wrong holder assignment** — see ph_mapping GAP above; can produce a same-holder remap
  that is actually cross-holder if the mapping overwrote incorrectly.
- **Sync time variability** — `ebay_products` ranged 87 s → 165 s on identical data due to
  MySQL server load (D08); monitor with `SHOW PROCESSLIST` if total sync exceeds ~300 s.
- **Cloudflare free-tunnel throughput (~1.5 Mbps)** — payloads must stay gzipped and small;
  do not reintroduce background-loading of all marketplaces (D06).
- **Schema-change deploys** — adding columns requires `DROP TABLE` + full re-sync, or INSERTs
  fail on column-count mismatch (D03 FAILURE-01, D06 FAILURE-04).
- **No automated tests** — verification is manual SQL; regressions in the velocity waterfall,
  dedup, or remap scoring would not be caught automatically.

---

## Reviewer Ownership

| Area | Reviewer | Scope |
|---|---|---|
| **Technical Reviewer** | Sync architecture & PostgreSQL logic | `db_sync.py`, table schemas, indexes, two-query split, sync sequencing |
| **Business Reviewer** | Remap business rules | thresholds, same-holder rule, parts priority, parent-SKU scoring (BUSINESS_RULES.md) |
| **Queryability Reviewer** | Documentation completeness | README, START-HERE, BUSINESS_RULES, HANDOVER, evidence inventory |
| **Coordinator** | Overall delivery | gap triage, deploy sign-off, AIOS asset promotion |

---

## Evidence Inventory

Day-wise records — store in `docs/evidence/` (commit pending if not yet present):

| Day | File | Status |
|---|---|---|
| D01 | `2026-05-27__sarujanan__ospm__REQ-01-D01.md` | COMPLETE |
| D02 | `2026-05-28__sarujanan__ospm__REMAP-01-D02.md` | COMPLETE |
| D03 | `2026-05-29__sarujanan__ospm__DETAIL-01-D03.md` | COMPLETE |
| D04 | `2026-06-01__sarujanan__ospm__REMAP-01-D04.md` | COMPLETE |
| D05 | `2026-06-02__sarujanan__ospm__REQ-01-D05.md` | COMPLETE |
| D06 | `2026-06-03__sarujanan__ospm__REMAP2-D06.md` | COMPLETE |
| D07 | `2026-06-04__sarujanan__ospm__REQ-01-D07.md` | IN_PROGRESS (closed by D08) |
| D08 | `2026-06-05__sarujanan__ospm__REQ-01-D08.md` | COMPLETE |
| D09 | `2026-06-08__sarujanan__ospm__REQ-01-D09.md` | COMPLETE |

---

## Recommended Next Steps (max 5)

1. **Commit the evidence** — add `docs/evidence/D01–D09` so the decision trail is
   version-controlled (currently the largest organization gap).
2. **Resolve the ph_mapping holder-conflict risk** — decide holder precedence and replace
   silent last-write-wins (highest correctness risk).
3. **Add automated tests** — cover the velocity waterfall, dedup, and remap P1/P2/P3 scoring
   to replace manual verification.
4. **Externalise thresholds** — move day thresholds to `stock_dashboard.yaml`, read by both
   dashboard and remap, removing the drift risk.
5. **Promote AIOS candidate assets** — extract the two-query split and sessionStorage
   server-synced cache patterns into a reusable `docs/patterns/` library.

---

## Queryability Validation

Can another developer or LLM, using this repo's docs, explain:

| Question | Verdict | Source |
|---|---|---|
| What was built? | **PASS** | README §Overview, START-HERE |
| Why it was built? | **PASS** | START-HERE §Project Overview |
| How it works? | **PASS** | README §Architecture/§Sync/§Remap |
| Business rules? | **PASS** | BUSINESS_RULES.md (single source) |
| Remaining gaps? | **PASS** | HANDOVER §Remaining Known Gaps |
| Next actions? | **PASS** | HANDOVER §Recommended Next Steps |

**Overall: PASS** — once `docs/evidence/` is committed (step 1), evidence-location and
gap queryability are fully satisfied from the repo alone, with no verbal explanation needed.
