"""Orders + customers + the full order lifecycle with role checks and an audit trail.

Received (offline: awaiting_approval -> approve/reject/request correction; online: auto-confirm
rules or pending_review with holds) -> confirmed -> in_production (department stages with
transfers) -> quality_check -> ready_for_dispatch -> dispatched.
Final output = last-stage good units only, so multi-department routes never double count.
"""

import re
from collections import defaultdict

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from lib.analytics import order_summary
from lib.authz import (APPROVERS, DISPATCH, EDITORS, PAYMENTS, PLANNERS, PRODUCERS, QC, READY, actor, current_user,
                       now_iso, require)
from lib.dates import today_iso
from lib.db import db
from lib.audit import run_audited_mutation
from lib.idempotency import run_idempotent_mutation
from lib.tenant import get_company_id
from lib.authz import require_permission
from lib.permissions import has_permission, require_any_permission
from models.core import (
    AssignIn, CustomerInput, CustomerRecord, CustomerStats, DecisionIn, DispatchIn, InspectIn, Order, OrderCreate,
    OrderList, OrderUpdate, Payment, PaymentIn, StageLog, StageSetup, TransferIn,
)
from routers.session import notify

router = APIRouter()

OPEN = ["awaiting_approval", "correction_requested", "pending_review", "confirmed", "in_production",
        "quality_check", "ready_for_dispatch"]
PRE_CONFIRM = {"awaiting_approval", "correction_requested", "pending_review"}


# ---------- customers ----------
@router.get("/customers", response_model=list[CustomerStats])
async def list_customers(search: str = "", cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "orders.read")
    q: dict = {"company_id": cid}
    if search.strip():
        rx = {"$regex": re.escape(search.strip()), "$options": "i"}
        q["$or"] = [{"name": rx}, {"city": rx}, {"contact_person": rx}, {"industry": rx}]
    custs = await db.customers.find(q, {"_id": 0}).sort("name", 1).to_list(5000)
    orders = await db.orders.find({"company_id": cid, "status": {"$ne": "rejected"}},
                                  {"_id": 0, "customer_id": 1, "status": 1, "quantity": 1, "unit_price": 1}).to_list(20000)
    stats: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])
    for o in orders:
        s = stats[o["customer_id"]]
        s[0] += 1
        s[1] += o["status"] in OPEN
        s[2] += o["quantity"] * o["unit_price"]
    return [CustomerStats(**c, order_count=stats[c["id"]][0], open_orders=stats[c["id"]][1],
                          total_value=round(stats[c["id"]][2], 2)) for c in custs]


@router.post("/customers", response_model=CustomerRecord, status_code=201)
async def create_customer(body: CustomerInput, request: Request,
                          cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "orders.manage")
    c = CustomerRecord(company_id=cid, created_on=today_iso(), **{**body.model_dump(), "name": body.name.strip()})

    async def create(session):
        if await db.customers.find_one(
                {"company_id": cid, "name": {"$regex": f"^{re.escape(body.name.strip())}$", "$options": "i"}},
                session=session):
            raise HTTPException(409, "a customer with this name already exists")
        await db.customers.insert_one(c.model_dump(), session=session)
        return c

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="customer.created",
        resource_type="customer", resource_id=c.id, mutate=create,
        after_state={"customer_id": c.id},
    )


@router.put("/customers/{customer_id}", response_model=CustomerRecord)
async def update_customer(customer_id: str, body: CustomerInput, request: Request,
                          cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "orders.manage")

    async def update(session):
        res = await db.customers.update_one({"company_id": cid, "id": customer_id},
                                            {"$set": body.model_dump()}, session=session)
        if not res.matched_count:
            raise HTTPException(404, "customer not found")
        return CustomerRecord(**await db.customers.find_one(
            {"company_id": cid, "id": customer_id}, {"_id": 0}, session=session))

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="customer.updated",
        resource_type="customer", resource_id=customer_id, mutate=update,
    )


# ---------- helpers ----------
def entry(stage: str, note: str, user: dict) -> dict:
    return {"stage": stage, "date": today_iso(), "note": note, "at": now_iso(), "user": user["name"], "role": user["role"]}


async def _load(cid: str, order_id: str) -> dict:
    doc = await db.orders.find_one({"company_id": cid, "id": order_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "order not found")
    return doc


async def _save(cid: str, o: dict, *entries: dict, user: dict, request: Request,
                insert: bool = False, precommit=None) -> Order:
    had_timeline = "timeline" in o
    expected_timeline = list(o.get("timeline", []))
    o["timeline"] = o.get("timeline", []) + list(entries)

    async def persist(session):
        if insert:
            await db.orders.insert_one(dict(o), session=session)
        else:
            selector = {"company_id": cid, "id": o["id"]}
            selector["timeline"] = expected_timeline if had_timeline else {"$exists": False}
            updated = await db.orders.replace_one(selector, o, session=session)
            if updated.matched_count != 1:
                exists = await db.orders.find_one({"company_id": cid, "id": o["id"]}, {"_id": 1}, session=session)
                if not exists:
                    raise HTTPException(404, "order not found")
                raise HTTPException(409, "order changed; refresh and retry")
        if precommit:
            await precommit(session)
        return Order(**o)

    latest_stage = entries[-1].get("stage", "updated") if entries else "updated"
    action = "order.lifecycle." + re.sub(r"[^a-z0-9]+", "_", latest_stage.casefold()).strip("_")
    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action=action,
        resource_type="order", resource_id=o["id"],
        after_state={"status": o.get("status"), "stage": latest_stage},
        mutate=persist,
    )


def _require_status(o: dict, *statuses: str) -> None:
    if o["status"] not in statuses:
        nice = lambda s: s.replace("_", " ")  # noqa: E731
        raise HTTPException(409, f"order is '{nice(o['status'])}'; this step needs {' or '.join(map(nice, statuses))}")


def _value(o: dict) -> float:
    return round(o["quantity"] * o["unit_price"], 2)


def demand(o: dict, k: int) -> int:
    """Good units stage k must deliver: the order quantity plus replacements for downstream scrap."""
    return o["quantity"] + sum(s["rejected"] for s in o["stages"][k + 1:])


def normalize(o: dict) -> None:
    st = o.get("stages") or []
    if not st:
        return
    last = len(st) - 1
    for i, s in enumerate(st):
        target = o["quantity"] if i == last else demand(o, i)
        out = s["produced"] if i == last else s["transferred"]
        s["status"] = "done" if out >= target else "active" if (i == 0 or s["received"] > 0) else "waiting"
    o["produced"] = st[last]["produced"]
    o["current_stage"] = next((i for i, s in enumerate(st) if s["status"] != "done"), last)
    o["department"] = st[o["current_stage"]]["department"]


async def _holds_for(cid: str, o: dict, company: dict) -> list[dict]:
    rules = company.get("auto_confirm") or {}
    if not rules.get("enabled", True):
        return [{"key": "manual", "label": "Automatic confirmation is switched off; manual confirmation required"}]
    holds = []
    if o["payment_terms"] == "advance":
        need = round(_value(o) * rules.get("advance_payment_pct", 30) / 100, 2)
        if o["amount_paid"] < need:
            holds.append({"key": "payment", "label": f"Advance payment of {need:,.2f} required before confirmation"})
    if rules.get("require_stock_check", True):
        fg = await db.inventory.find_one({"company_id": cid, "category": "finished", "name": o["product"]})
        if not fg or fg["quantity"] < o["quantity"]:
            holds.append({"key": "stock", "label": "Stock / material review: quantity exceeds finished-goods stock"})
    if o.get("customization", "").strip():
        holds.append({"key": "customization", "label": "Custom specification needs engineering review"})
    if _value(o) > rules.get("max_order_value", 25000):
        holds.append({"key": "value_limit", "label": f"Order value above auto-confirm limit ({rules.get('max_order_value', 25000):,.0f})"})
    if rules.get("repeat_customers_only", True) and not await db.orders.find_one(
            {"company_id": cid, "customer_id": o["customer_id"], "status": "dispatched"}):
        holds.append({"key": "new_customer", "label": "First order from this customer; manual confirmation required"})
    return [dict(h, cleared=False, cleared_by=None, cleared_at=None) for h in holds]


def _maybe_confirm(o: dict, user_name: str) -> list[dict]:
    if o["status"] == "pending_review" and all(h["cleared"] for h in o["holds"]):
        o["status"] = "confirmed"
        return [{"stage": "Confirmed", "date": today_iso(), "note": "All review conditions satisfied", "at": now_iso(),
                 "user": user_name, "role": "system"}]
    return []


async def _move_finished_stock(cid: str, product: str, delta: int, *, session=None) -> None:
    item = await db.inventory.find_one({"company_id": cid, "category": "finished", "name": product}, session=session)
    if item:
        await db.inventory.update_one(
            {"_id": item["_id"]},
            [{"$set": {"quantity": {"$max": [0.0, {"$add": [{"$ifNull": ["$quantity", 0.0]}, delta]}]}}}],
            session=session,
        )


# ---------- orders ----------
@router.get("/orders", response_model=OrderList)
async def list_orders(search: str = "", status: str = "all", priority: str = "all", overdue: bool = False,
                      department: str = "all", source: str = "all", cid: str = Depends(get_company_id),
                      user: dict = Depends(current_user)):
    require_any_permission(user, "orders.read", "orders.department.read")
    q: dict = {"company_id": cid}
    department_ids = user.get("department_ids") or ([user.get("department")] if user.get("department") else [])
    department_scoped = not has_permission(user, "orders.read")
    if department_scoped:
        q["$or"] = [
            {"department": {"$in": department_ids}},
            {"stages.department": {"$in": department_ids}},
        ]
    today = today_iso()
    if status == "pending":
        q["status"] = {"$in": OPEN}
    elif status == "received":
        q["status"] = {"$in": list(PRE_CONFIRM)}
    elif status == "active":
        q["status"] = {"$in": ["in_production", "quality_check"]}
    elif status != "all":
        q["status"] = status
    for k, v in (("priority", priority), ("department", department), ("source", source)):
        if v != "all":
            q[k] = v
    if overdue:
        q["status"] = {"$in": OPEN}
        q["expected_delivery"] = {"$lt": today}
    if search.strip():
        rx = {"$regex": re.escape(search.strip()), "$options": "i"}
        search_filter = {"$or": [{"order_no": rx}, {"customer.name": rx}, {"product": rx}, {"customer.city": rx}]}
        if "$or" in q:
            q["$and"] = [{"$or": q.pop("$or")}, search_filter]
        else:
            q.update(search_filter)
    docs = await db.orders.find(q, {"_id": 0}).sort([("order_date", -1), ("order_no", -1)]).to_list(2000)
    return OrderList(items=[Order(**d) for d in docs], summary=await order_summary(
        cid, today, department_ids=department_ids if department_scoped else None))


@router.get("/orders/{order_id}", response_model=Order)
async def get_order(order_id: str, cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    if has_permission(user, "orders.read"):
        return Order(**await _load(cid, order_id))
    require_permission(user, "orders.department.read")
    departments = user.get("department_ids") or ([user.get("department")] if user.get("department") else [])
    doc = await db.orders.find_one({"company_id": cid, "id": order_id, "$or": [
        {"department": {"$in": departments}}, {"stages.department": {"$in": departments}},
    ]}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="order not found")
    return Order(**doc)


@router.post("/orders", response_model=Order, status_code=201)
async def create_order(body: OrderCreate, request: Request,
                       user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    require_permission(user, "orders.create")
    cust = await db.customers.find_one({"company_id": cid, "id": body.customer_id}, {"_id": 0})
    if not cust:
        raise HTTPException(404, "customer not found")
    company = await db.companies.find_one({"id": cid}, {"_id": 0}) or {}
    today = today_iso()
    meta = await db.meta.find_one_and_update({"company_id": cid}, {"$inc": {"order_seq": 1}}, upsert=True, return_document=True)
    seq = (meta or {}).get("order_seq", 1)
    data = body.model_dump()
    advance = data.pop("advance_paid")
    o = Order(company_id=cid, order_no=f"SO-{today[:4]}-{seq:04d}", status="awaiting_approval", order_date=today,
              customer={k: cust.get(k, "") for k in ("name", "contact_person", "email", "phone", "city")},
              submitted_by=actor(user), submitted_at=now_iso(), **data).model_dump()
    via = "online order form" if body.source == "online" else "offline order request"
    entries = [entry("Order received", f"{body.quantity} x {body.product} via {via}", user)]
    if advance > 0:
        o["payments"] = [Payment(amount=advance, date=today, method="Advance", recorded_by=user["name"]).model_dump()]
        o["amount_paid"] = advance
        entries.append(entry("Payment recorded", f"{advance:,.2f} advance", user))
    if body.source == "offline":
        entries.append(entry("Submitted for approval", f"Submitted by {user['name']}", user))
        await notify(cid, list(APPROVERS), f"Order {o['order_no']} awaiting approval",
                     f"{user['name']} submitted {body.quantity} x {body.product} for {cust['name']}", f"/orders?order={o['id']}")
    else:
        o["holds"] = await _holds_for(cid, o, company)
        if o["holds"]:
            o["status"] = "pending_review"
            entries.append(entry("Pending review", "; ".join(h["label"] for h in o["holds"]),
                                 {"name": "Auto-confirmation rules", "role": "system"}))
            await notify(cid, list(APPROVERS), f"Online order {o['order_no']} needs review",
                         f"{len(o['holds'])} condition(s) before confirmation", f"/orders?order={o['id']}")
        else:
            o["status"] = "confirmed"
            entries.append(entry("Confirmed", "Auto-confirmed: all rules satisfied", {"name": "Auto-confirmation rules", "role": "system"}))
    o.pop("_id", None)
    return await _save(cid, o, *entries, user=user, request=request, insert=True)


@router.patch("/orders/{order_id}", response_model=Order)
async def update_order(order_id: str, body: OrderUpdate, request: Request,
                       user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    o = await _load(cid, order_id)
    patch = body.model_dump(exclude_none=True)
    core = {k: patch[k] for k in ("customer_id", "product", "quantity", "unit_price", "customization") if k in patch}
    require_permission(user, "orders.manage")
    if core:
        require_permission(user, "orders.manage")
        _require_status(o, *PRE_CONFIRM)
        if "customer_id" in core:
            cust = await db.customers.find_one({"company_id": cid, "id": core["customer_id"]}, {"_id": 0})
            if not cust:
                raise HTTPException(404, "customer not found")
            o["customer"] = {k: cust.get(k, "") for k in ("name", "contact_person", "email", "phone", "city")}
    if not patch:
        return Order(**o)
    o.update(patch)
    return await _save(cid, o, entry("Order edited", ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in patch.items()), user),
                       user=user, request=request)


@router.post("/orders/{order_id}/resubmit", response_model=Order)
async def resubmit(order_id: str, request: Request, user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    require_permission(user, "orders.manage")
    o = await _load(cid, order_id)
    _require_status(o, "correction_requested")
    o["status"] = "awaiting_approval"
    await notify(cid, list(APPROVERS), f"Order {o['order_no']} resubmitted", f"Corrected by {user['name']}", f"/orders?order={o['id']}")
    return await _save(cid, o, entry("Resubmitted for approval", "Corrections made", user), user=user, request=request)


@router.post("/orders/{order_id}/decision", response_model=Order)
async def decide(order_id: str, body: DecisionIn, request: Request,
                 user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    require_permission(user, "orders.approve")
    o = await _load(cid, order_id)
    _require_status(o, "awaiting_approval")
    reason = body.reason.strip()
    if body.decision in ("reject", "request_correction") and len(reason) < 3:
        raise HTTPException(422, "a reason is required" if body.decision == "reject" else "describe the corrections needed")
    status, decision, stage = {"approve": ("confirmed", "approved", "Approved"), "reject": ("rejected", "rejected", "Rejected"),
                               "request_correction": ("correction_requested", "correction_requested", "Correction requested")}[body.decision]
    o.update(status=status, decision=decision, decided_by=actor(user), decided_at=now_iso(), decision_note=reason)
    if body.decision != "approve":
        await notify(cid, ["sales_staff", "owner"], f"Order {o['order_no']} {stage.lower()}", reason, f"/orders?order={o['id']}")
    return await _save(cid, o, entry(stage, reason or "Order confirmed for production", user), user=user, request=request)


@router.post("/orders/{order_id}/holds/{key}/clear", response_model=Order)
async def clear_hold(order_id: str, key: str, request: Request,
                     user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    require_permission(user, "orders.approve")
    o = await _load(cid, order_id)
    _require_status(o, "pending_review")
    h = next((h for h in o["holds"] if h["key"] == key and not h["cleared"]), None)
    if not h:
        raise HTTPException(404, "no open condition with that key")
    h.update(cleared=True, cleared_by=user["name"], cleared_at=now_iso())
    e = entry("Review condition cleared", h["label"], user)
    return await _save(cid, o, e, *_maybe_confirm(o, user["name"]), user=user, request=request)


@router.post("/orders/{order_id}/payments", response_model=Order)
async def add_payment(order_id: str, body: PaymentIn, request: Request,
                      idempotency_key: str = Header(..., alias="Idempotency-Key"),
                      user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    require_permission(user, "orders.manage")

    async def record_payment(session):
        o = await db.orders.find_one({"company_id": cid, "id": order_id}, {"_id": 0}, session=session)
        if not o:
            raise HTTPException(404, "order not found")
        if o["status"] == "rejected":
            raise HTTPException(409, "cannot record payments on a rejected order")
        if o["amount_paid"] + body.amount > _value(o) + 0.01:
            raise HTTPException(422, f"payment exceeds the outstanding balance ({_value(o) - o['amount_paid']:,.2f})")

        expected_amount_paid = o["amount_paid"]
        o["payments"].append(Payment(amount=body.amount, date=today_iso(), method=body.method,
                                     reference=body.reference, recorded_by=user["name"]).model_dump())
        o["amount_paid"] = round(o["amount_paid"] + body.amount, 2)
        entries = [entry("Payment recorded", f"{body.amount:,.2f} via {body.method}"
                          + (f" · {body.reference}" if body.reference else ""), user)]
        company = await db.companies.find_one({"id": cid}, {"_id": 0}, session=session) or {}
        need = _value(o) * (company.get("auto_confirm") or {}).get("advance_payment_pct", 30) / 100
        for hold in o["holds"]:
            if hold["key"] == "payment" and not hold["cleared"] and o["amount_paid"] >= need - 0.01:
                hold.update(cleared=True, cleared_by="Payment received", cleared_at=now_iso())
                entries.append(entry("Review condition cleared", hold["label"], user))
        o["timeline"] = o.get("timeline", []) + entries + _maybe_confirm(o, user["name"])
        result = await db.orders.replace_one(
            {"company_id": cid, "id": order_id, "amount_paid": expected_amount_paid},
            o,
            session=session,
        )
        if result.matched_count != 1:
            raise HTTPException(409, "order state changed; refresh and retry")
        return Order(**o)

    return await run_idempotent_mutation(
        company_id=cid, actor_id=user["id"], operation=f"order.payment:{order_id}",
        key=idempotency_key, payload=body.model_dump(mode="json"), mutate=record_payment,
        user=user, request=request, audit_action="order.payment_recorded", audit_resource_type="order",
    )


@router.post("/orders/{order_id}/assign", response_model=Order)
async def assign(order_id: str, body: AssignIn, request: Request,
                 user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    require_permission(user, "job_cards.manage")
    o = await _load(cid, order_id)
    _require_status(o, "confirmed")
    company = await db.companies.find_one({"id": cid}, {"_id": 0}) or {}
    valid = set(company.get("departments", []))
    bad = [d for d in body.departments if d not in valid]
    if bad or len(set(body.departments)) != len(body.departments):
        raise HTTPException(422, f"invalid or repeated departments: {', '.join(bad) or 'duplicates'}")
    stages = []
    for i, d in enumerate(body.departments):
        setup = body.stages[i] if i < len(body.stages) else StageSetup()
        sup = setup.supervisor or (await db.users.find_one({"company_id": cid, "role": "supervisor", "department": d}) or {}).get("name")
        stages.append({"department": d, "supervisor": sup, "assigned_employees": setup.assigned_employees,
                       "target_date": setup.target_date, "received": o["quantity"] if i == 0 else 0,
                       "produced": 0, "rejected": 0, "transferred": 0, "status": "waiting"})
    o.update(status="in_production", stages=stages, route_name=body.route_name or " → ".join(body.departments), qc_status=None,
             production_target_date=max((s["target_date"] for s in stages if s["target_date"]), default=None))
    normalize(o)

    async def _check_no_active_job_card(session):
        if await db.job_cards.find_one(
            {
                "company_id": cid,
                "order_id": order_id,
                "$or": [
                    {"status": {"$ne": "superseded"}},
                    {"status": {"$type": "array"}},
                ],
            },
            {"_id": 1},
            session=session,
        ):
            raise HTTPException(409, "order has an active job card and cannot be assigned via legacy stages")

    return await _save(cid, o, entry("Assigned to production", f"Route: {' → '.join(body.departments)}; first stage {stages[0]['department']}"
                                     + (f" (supervisor {stages[0]['supervisor']})" if stages[0]["supervisor"] else ""), user),
                       user=user, request=request, precommit=_check_no_active_job_card)


@router.put("/orders/{order_id}/stages/{k}", response_model=Order)
async def update_stage(order_id: str, k: int, body: StageSetup, request: Request,
                       user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    o = await _load(cid, order_id)
    if not 0 <= k < len(o["stages"]):
        raise HTTPException(404, "stage not found")
    s = o["stages"][k]
    if not has_permission(user, "job_cards.manage"):
        require_permission(user, "job_cards.department.manage", s["department"])
    s.update(supervisor=body.supervisor or s["supervisor"], assigned_employees=body.assigned_employees, target_date=body.target_date)
    team = ", ".join(body.assigned_employees) or "no team"
    return await _save(cid, o, entry("Stage assignment updated", f"{s['department']}: supervisor {s['supervisor']}, team {team}"
                                     + (f", target {body.target_date}" if body.target_date else ""), user),
                       user=user, request=request)


@router.post("/orders/{order_id}/stages/{k}/log", response_model=Order)
async def log_stage(order_id: str, k: int, body: StageLog, request: Request,
                    user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    o = await _load(cid, order_id)
    if not 0 <= k < len(o.get("stages", [])):
        raise HTTPException(404, "stage not found")
    s = o["stages"][k]
    require_permission(user, "production.department.write", s["department"])
    _require_status(o, "in_production")
    if body.produced + body.rejected == 0:
        raise HTTPException(422, "enter produced or rejected units")
    if s["status"] == "waiting":
        raise HTTPException(409, f"{s['department']} has not received any units yet")
    if s["produced"] + body.produced > demand(o, k):
        raise HTTPException(422, f"only {demand(o, k) - s['produced']} more good units are needed at {s['department']}")
    if k > 0 and s["produced"] + s["rejected"] + body.produced + body.rejected > s["received"]:
        raise HTTPException(422, f"{s['department']} has only {s['received'] - s['produced'] - s['rejected']} received units left to process")
    s["produced"] += body.produced
    s["rejected"] += body.rejected
    normalize(o)
    # department throughput for production reports (the order's final output is counted once, from the last stage)
    mq: dict = {"company_id": cid, "id": body.machine_id} if body.machine_id else {"company_id": cid, "department": s["department"]}
    machine = await db.machines.find_one(mq, sort=[("status", -1), ("code", 1)])
    today = today_iso()
    async def update_production_summary(session):
        if machine:
            await db.production.update_one(
                {"company_id": cid, "date": today, "machine_id": machine["id"]},
                {"$inc": {"actual": body.produced + body.rejected, "rejected": body.rejected},
                 "$setOnInsert": {"id": f"{machine['id']}-{today}",
                                  "department": machine["department"], "planned": 0}},
                upsert=True, session=session,
            )
    pending = max(0, demand(o, k) - s["produced"])
    entries = [entry("Production update", f"{s['department']}: +{body.produced} good, +{body.rejected} rejected; "
                     f"stage total {s['produced']} good / {s['rejected']} rejected, {pending} pending"
                     + (f" on {machine['code']}" if machine else "") + (f" · {body.note}" if body.note else ""), user)]
    last = len(o["stages"]) - 1
    if k == last and s["produced"] >= o["quantity"]:
        o["status"], o["qc_status"] = "quality_check", "pending"
        entries.append(entry("Sent to quality control", f"{s['produced']} finished units ready for inspection", user))
    return await _save(cid, o, *entries, user=user, request=request, precommit=update_production_summary)


@router.post("/orders/{order_id}/stages/{k}/transfer", response_model=Order)
async def transfer(order_id: str, k: int, body: TransferIn, request: Request,
                   idempotency_key: str = Header(..., alias="Idempotency-Key"),
                   user: dict = Depends(current_user), cid: str = Depends(get_company_id)):

    # Authorization must run before idempotency lookup. Otherwise a cached
    # result could be returned after the actor's department access is revoked.
    route_order = await db.orders.find_one(
        {"company_id": cid, "id": order_id}, {"stages": 1},
    )
    if not route_order:
        raise HTTPException(404, "order not found")
    route_stages = route_order.get("stages", [])
    if not 0 <= k < len(route_stages) - 1:
        raise HTTPException(404, "no next stage to transfer to")
    require_permission(user, "production.department.write", route_stages[k]["department"])

    async def apply_transfer(session):
        o = await db.orders.find_one({"company_id": cid, "id": order_id}, {"_id": 0}, session=session)
        if not o:
            raise HTTPException(404, "order not found")
        if not 0 <= k < len(o.get("stages", [])) - 1:
            raise HTTPException(404, "no next stage to transfer to")
        s, nxt = o["stages"][k], o["stages"][k + 1]
        require_permission(user, "production.department.write", s["department"])
        _require_status(o, "in_production")
        available = s["produced"] - s["transferred"]
        if body.quantity > available:
            raise HTTPException(422, f"only {available} good units at {s['department']} are available to transfer")

        old_transferred, old_received = s["transferred"], nxt["received"]
        s["transferred"] += body.quantity
        nxt["received"] += body.quantity
        normalize(o)
        event = entry("Units transferred", f"{body.quantity} units {s['department']} → {nxt['department']} "
                      f"(received there: {nxt['received']})", user)
        o["timeline"] = o.get("timeline", []) + [event]
        result = await db.orders.replace_one(
            {"company_id": cid, "id": order_id, "status": "in_production",
             f"stages.{k}.transferred": old_transferred,
             f"stages.{k + 1}.received": old_received},
            o,
            session=session,
        )
        if result.matched_count != 1:
            raise HTTPException(409, "order state changed; refresh and retry")
        return Order(**o)

    return await run_idempotent_mutation(
        company_id=cid, actor_id=user["id"], operation=f"order.transfer:{order_id}:{k}",
        key=idempotency_key, payload=body.model_dump(mode="json"), mutate=apply_transfer,
        user=user, request=request, audit_action="production.transfer_recorded", audit_resource_type="order",
    )


@router.post("/orders/{order_id}/inspect", response_model=Order)
async def inspect(order_id: str, body: InspectIn, request: Request,
                  user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    o = await _load(cid, order_id)
    require_permission(user, "quality.inspect")
    _require_status(o, "quality_check")
    awaiting = o["produced"] - o["qc_passed"]
    if body.passed + body.rejected == 0 or body.passed + body.rejected > awaiting:
        raise HTTPException(422, f"inspect between 1 and {awaiting} units")
    o["qc_passed"] += body.passed
    o["qc_rejected"] += body.rejected
    o["qc_inspector"] = user["name"]
    note = f"{body.passed} passed, {body.rejected} rejected" + (f" · {body.notes}" if body.notes else "")
    if o["qc_passed"] >= o["quantity"]:
        o["qc_status"] = "passed"
        return await _save(cid, o, entry("Quality inspection passed", note, user), user=user, request=request)
    if body.rejected:
        if o["stages"]:
            o["stages"][-1]["produced"] -= body.rejected  # rejected units go back to the last stage for rework
        o["status"], o["qc_status"] = "in_production", "rework"
        normalize(o)
        return await _save(cid, o, entry("Quality inspection: rework required", f"{note}; {body.rejected} units back to {o['department']}", user),
                           user=user, request=request)
    return await _save(cid, o, entry("Quality inspection (partial)", note, user), user=user, request=request)


@router.post("/orders/{order_id}/ready", response_model=Order)
async def mark_ready(order_id: str, request: Request, idempotency_key: str = Header(..., alias="Idempotency-Key"),
                     user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    require_permission(user, "finished_goods.manage")
    async def transition_ready(session):
        o = await db.orders.find_one({"company_id": cid, "id": order_id}, {"_id": 0}, session=session)
        if not o:
            raise HTTPException(404, "order not found")
        _require_status(o, "quality_check")
        if o.get("qc_status") != "passed":
            raise HTTPException(409, f"quality inspection not complete ({o['qc_passed']}/{o['quantity']} passed)")
        o["status"] = "ready_for_dispatch"
        event = entry("Ready for dispatch", f"{o['quantity']} units moved to finished goods", user)
        o["timeline"] = o.get("timeline", []) + [event]
        result = await db.orders.replace_one(
            {"company_id": cid, "id": order_id, "status": "quality_check", "qc_status": "passed"},
            o, session=session,
        )
        if result.matched_count != 1:
            raise HTTPException(409, "order state changed; refresh and retry")
        await _move_finished_stock(cid, o["product"], o["quantity"], session=session)
        return Order(**o)

    return await run_idempotent_mutation(
        company_id=cid, actor_id=user["id"], operation=f"order.ready:{order_id}",
        key=idempotency_key, payload={}, mutate=transition_ready,
        user=user, request=request, audit_action="finished_goods.ready", audit_resource_type="order",
    )


@router.post("/orders/{order_id}/dispatch", response_model=Order)
async def dispatch(order_id: str, body: DispatchIn, request: Request,
                   idempotency_key: str = Header(..., alias="Idempotency-Key"),
                   user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    require_permission(user, "finished_goods.manage")
    async def transition_dispatch(session):
        o = await db.orders.find_one({"company_id": cid, "id": order_id}, {"_id": 0}, session=session)
        if not o:
            raise HTTPException(404, "order not found")
        _require_status(o, "ready_for_dispatch")
        o.update(status="dispatched", dispatched_date=today_iso(), dispatch_carrier=body.carrier,
                 dispatch_ref=body.reference)
        event = entry("Dispatched", body.carrier + (f" · {body.reference}" if body.reference else "")
                      + (f" · {body.notes}" if body.notes else ""), user)
        o["timeline"] = o.get("timeline", []) + [event]
        result = await db.orders.replace_one(
            {"company_id": cid, "id": order_id, "status": "ready_for_dispatch"}, o, session=session,
        )
        if result.matched_count != 1:
            raise HTTPException(409, "order state changed; refresh and retry")
        await _move_finished_stock(cid, o["product"], -o["quantity"], session=session)
        return Order(**o)

    return await run_idempotent_mutation(
        company_id=cid, actor_id=user["id"], operation=f"order.dispatch:{order_id}",
        key=idempotency_key, payload=body.model_dump(mode="json"), mutate=transition_dispatch,
        user=user, request=request, audit_action="finished_goods.dispatched", audit_resource_type="order",
    )
