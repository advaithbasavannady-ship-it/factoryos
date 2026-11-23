# FactoryOS — Owner Application (prototype)

Standalone demo of a universal factory-management SaaS. Fictional tenant **Apex Precision Industries** (general manufacturing, Columbus OH, USD, unit "units"). No login: the app opens on the Overview as Demo Owner **Marcus Vance, Managing Director**. No external integrations (ERP, biometric, IoT, AI); everything is entered in-app and persisted in Mongo.

## Architecture
- Backend FastAPI: `backend/routers/*` (company, orders+customers, attendance, production, machines, inventory, schedule, assistant, dashboard, reports), models in `backend/models/core.py` + `workspace.py`, mirrored in `frontend/src/lib/types.ts`.
- Multi-tenant ready: every doc has `company_id`; `lib/tenant.get_company_id()` returns DEFAULT_COMPANY_ID (`apex-precision`). Future auth / Workforce app resolve the tenant there.
- Demo data: `lib/seed_data.py`. Seeds on first boot; `ensure_fresh()` (router dependency) tops up attendance, production and orders for each new day. `POST /api/demo/reset` (Settings → Reset demo data) or `python seed.py` reseeds relative to today.
- AI: `lib/assistant_provider.py` `NotConnectedProvider`. It always replies "AI integration is not connected yet" with a module link, never factory data. Swap in an LLM provider later.
- Frontend: `components/AppShell.tsx` (sidebar + topbar), `pages/*.tsx`, `components/OrderSheet.tsx` (workflow), `lib/queries.ts` (`useApi` keyed by path; `useAction` invalidates ALL queries on success so dashboards/reports update).

## Order workflow v2 (lifecycle, approvals, departments, audit)
Statuses: awaiting_approval | correction_requested | rejected | pending_review → confirmed → in_production → quality_check → ready_for_dispatch → dispatched.
- Offline orders (source=offline) always start `awaiting_approval`; approvers (owner, manager) are notified (`notifications` collection) and can POST `/orders/{id}/decision` {approve|reject|request_correction, reason}. A reason is required for reject/correction. `correction_requested` → PATCH edits + POST `/resubmit`.
- Online orders are checked against `company.auto_confirm` rules (max value, repeat customers only, advance %, stock check, customization). If any hold applies → `pending_review` with `holds[]`; `/holds/{key}/clear` (approvers) or a payment (`/payments`) clears them, and the order auto-confirms when all are clear.
- Order value (qty × price) is separate from `amount_paid`/`payments[]`.
- `/assign` {departments[], route_name} (owner, production_manager) builds `stages[]` (department, supervisor, team, target, received/produced/rejected/transferred). Routes are configurable in Settings (`company.routes`).
- `/stages/{k}/log` {produced, rejected} (owner, PM, or supervisor of that department); `/stages/{k}/transfer` {quantity} moves good units to the next stage. Validation: produced ≤ demand (qty + downstream scrap); stage k>0 cannot process more than received. Last stage reaching qty → auto `quality_check`.
- `/inspect` {passed, rejected} (owner, qc_inspector); rejected units go back to the last stage (rework). `/ready` (owner, manager, qc, dispatch) once qc_status=passed (+finished stock). `/dispatch` (owner, manager, dispatch_clerk) (−finished stock).
- `order.produced` = last-stage good units only (no double counting); dashboard `order_summary.units_produced` sums that.
- Every step appends `timeline[]` {stage, note, at (ISO), user, role}.
- Migration `seed_data.upgrade_v2` upgraded legacy orders in place (meta.schema=2) and added showcase orders (3 awaiting approval, 1 correction, 1 rejected, 2 pending review, 1 four-department order mid-production).

## Roles (demo identity, no real login)
The top-bar "Acting as" switcher → POST `/api/session/act-as` sets the httpOnly cookie `factoryos_user`; no cookie = owner. Backend checks in `lib/authz.py`. Users: u-owner Marcus Vance (owner), u-ops Sarah Mitchell (manager), u-pm Daniel Brooks (production_manager), u-sup-{machining,fabrication,assembly,finishing,packaging} (supervisor, department-scoped), u-qc (qc_inspector), u-dispatch (dispatch_clerk), u-sales Emily Ross (sales_staff).

## Legacy order workflow notes
new → (POST /orders/{id}/start-production {department}) in_production → (POST /progress {quantity, rejected, machine_id?}, adds to produced and to today's production record of the dept machine; auto-moves to quality_check when produced ≥ quantity) → (POST /inspect {passed, rejected, inspector}; passed ≥ qty → completed (+finished-goods stock), else back to in_production for rework) → (POST /dispatch {carrier, reference}; completed only, −finished-goods stock) dispatched. Wrong-stage calls → 409.
Pending = new + in_production + quality_check + completed. Overdue = not dispatched and expected_delivery < today.

## Seed facts
- 12 customers, ~110 orders over 60 days; 5 orders received today; ~2–3 dispatched today.
- 48 employees in 7 departments; 45 days attendance (present/late/absent/leave).
- 12 machines (statuses are SIMULATED): CNC-03 + PKG-01 maintenance; PRB-01, ASM-02, CMP-01 idle; others running.
- 10 raw materials (several low stock e.g. RM-1003, RM-1004, RM-1007), 8 finished goods, 6 suppliers, 14 POs.
- ~15 events from −5 to +12 days; 4 today.
- Production departments: Machining, Fabrication, Assembly, Finishing, Packaging.

## Reports
`GET /api/reports/{production_daily|production_weekly|performance_monthly|attendance|orders|machines|inventory}?start&end` → columns/rows/chart/totals; CSV is generated client-side from rows.
