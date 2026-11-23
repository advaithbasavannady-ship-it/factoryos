# FactoryOS engineering roadmap

Last updated: 2026-11-12. Factory-specific behavior remains subject to product-owner confirmation.

## Current status

Phase 1.2 foundation passed the previously executed isolated integration suite, but is not approved. The distinct-request-ID refresh race regression passed against the isolated Mongo replica set (1/1). See `TESTING_STATUS.md` and `KNOWN_ISSUES.md`.

## Phase 1.2 remaining gates

- Desktop Chrome Playwright coverage for simultaneous refresh passed 1/1 with API stubs; wider browser/OS CI coverage remains open.
- A fifth server-backed refresh concurrency case for simultaneous use of one old token with distinct request IDs passed 1/1 separately against the isolated replica set; the previous four cases also passed earlier. Docker isolation was reverified before this run.
- Production MongoDB topology and failover were not verified; local README documents a development-only local mongod.
- Repository client search found no operational frontend client or Excel import/export flow; deployed external callers require an owner inventory before release.
- Failed-login and generic middleware audit remain best-effort; the email outbox and department field-visibility proposal are documented in `PHASE_1_2_REMAINING_GATES.md` but not implemented.
- The clean isolated idempotency collection has zero records; do not infer production growth or enable TTL cleanup from that baseline.
- Concise alternatives, risks, recommendations and owner decisions are in `PHASE_1_2_OWNER_DECISIONS.md`; these are proposals only.

## Roadmap

| Phase | Scope | Dependencies | Priority | Completion criteria |
|---|---|---|---|---|
| 1.2 reliability closeout | Authentication concurrency, audit reliability, tenant/department tests, atomic business mutations, deployment gates | Existing security foundation and isolated Mongo replica set | P0/P1 | Defects reproduced and fixed; real-Mongo regression tests pass; risks and approval gates resolved |
| 2A foundation and schemas | Tenant-safe APIs, versioned records, idempotency, durable audit, migrations and restore gates | Phase 1 approval | P0 | Mutation contract and migration/rollback proof approved |
| 2B employee and department management | Membership lifecycle, departments, roles, assignment and invitations | 2A and visibility decisions | P1 | Active-membership authorization; tested read/write scoping |
| 2C orders and Job Cards | Online/offline orders, multi-card linkage, versioned Job Cards, original-document preservation and reviewable OCR | GCPL forms and unresolved field meanings | P1 | GCPL reference coverage validated; no invented column meanings; revision-linked lifecycle verified |
| 2D production | 24 established stages, WIP, partial transfer, split lots, rework, rejection and final acceptance | Factory-approved quantity/rework rules and 2A event contract | P1 | Quantity lineage and reconciliation pass factory scenarios |
| 2E inventory and purchasing | Batch/lot stock, receipts/issues/returns, suppliers/invoices, dates, locations, QC, historical reporting, reservations | Posting, costing, FIFO/FEFO and backdating decisions | P1 | Immutable movement ledger; effective/as-known reports; concurrency and reconciliation tests |
| 2F material requests | Requested, approved, reserved, issued, consumed, returned and wasted states | Inventory event contract and department approval rules | P1 | No reservation reduces physical stock before issue; complete quantity lineage |
| 2G scrap | Rejection distinction, candidate/collection, scrap stock, reuse/sale/dispatch/disposal | Factory classification and disposition rules | P1 | No automatic rejection-to-scrap conversion; reconciliation tested |
| 2H finished goods | QA acceptance, stock, specification/revision linkage, reservation, dispatch and repeat-order matching | QC, order and inventory contracts | P1 | Concurrent reservation/dispatch prevents double allocation |
| 2I Excel integration | Multi-sheet import/export, mappings, preview, validation, duplicate/changed-row detection, row errors, originals and audit | Approved templates and import ownership rules | P1 | Idempotent, traceable round trips on representative workbooks |
| 2J department tables and messaging | Dynamic tables, search, notifications, linked communication and attachments | Department visibility and attachment policy | P2 | Messages cannot mutate transactions; tenant and attachment tests pass |
| 2K reporting and CEO dashboard | Consolidated and departmental reports; period selection; source and generation metadata | Data definitions and visibility decisions | P2 | Reconciled sample reports; clickable metrics; appropriate field-level access |
| Release readiness | Backup/restore, monitoring, security review, deployment config and operational runbooks | All critical product modules | P0 | Recovery drill, security gates, migration evidence, explicit product-owner approval |

## Dependency rules

- Do not implement financial, inventory valuation, production quantity, scrap disposition, or Job Card column semantics without factory-owner decisions.
- Preserve existing useful application code and established workflows.
- Keep test, development and production environments separate; no production or demo database access is authorized by this roadmap.
- Do not initialize Git or make destructive repository changes without explicit approval.
