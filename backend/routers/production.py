from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from lib.analytics import group_by, production_records, sum_records, week_start
from lib.audit import run_audited_mutation
from lib.dates import today_iso
from lib.db import db
from lib.tenant import get_company_id
from lib.authz import current_user, require_any_permission, require_permission
from lib.permissions import has_permission
from models.core import (
    DeptProduction, MachineProduction, ProductionLog, ProductionPoint, ProductionRecord, ProductionSummary,
)

router = APIRouter(prefix="/production")


@router.get("/summary", response_model=ProductionSummary)
async def production_summary(period: str = "today", cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_any_permission(user, "production.company.read", "production.department.read")
    company_wide = has_permission(user, "production.company.read")
    departments = user.get("department_ids") or ([user.get("department")] if user.get("department") else [])
    company = await db.companies.find_one({"id": cid}, {"_id": 0, "production_unit": 1})
    today = date.fromisoformat(today_iso())
    t = today.isoformat()
    wk = week_start(today).isoformat()
    mo = today.replace(day=1).isoformat()
    recs = await production_records(cid, (today - timedelta(days=200)).isoformat(), t)
    if not company_wide:
        recs = [record for record in recs if record.get("department") in departments]

    daily_start = (today - timedelta(days=29)).isoformat()
    by_day = group_by([r for r in recs if r["date"] >= daily_start], lambda r: r["date"])
    by_week = group_by([r for r in recs if r["date"] >= (week_start(today) - timedelta(weeks=11)).isoformat()],
                       lambda r: week_start(date.fromisoformat(r["date"])).isoformat())
    by_month = group_by(recs, lambda r: r["date"][:7])

    start = {"today": t, "week": wk, "month": mo}.get(period, t)
    period_recs = [r for r in recs if r["date"] >= start]
    machine_query = {"company_id": cid}
    if not company_wide:
        machine_query["department"] = {"$in": departments}
    machines = await db.machines.find(machine_query, {"_id": 0}).to_list(1000)
    by_machine = group_by(period_recs, lambda r: r["machine_id"])

    return ProductionSummary(
        unit=(company or {}).get("production_unit", "units"),
        period=period,
        today=sum_records([r for r in recs if r["date"] == t]),
        week=sum_records([r for r in recs if r["date"] >= wk]),
        month=sum_records([r for r in recs if r["date"] >= mo]),
        daily=[ProductionPoint(label=d, **sum_records(v).model_dump()) for d, v in sorted(by_day.items())],
        weekly=[ProductionPoint(label=d, **sum_records(v).model_dump()) for d, v in sorted(by_week.items())],
        monthly=[ProductionPoint(label=d, **sum_records(v).model_dump()) for d, v in sorted(by_month.items())][-6:],
        departments=[DeptProduction(department=d, **sum_records(v).model_dump())
                     for d, v in sorted(group_by(period_recs, lambda r: r["department"]).items())],
        machines=[MachineProduction(machine_id=m["id"], code=m["code"], name=m["name"], department=m["department"],
                                    **sum_records(by_machine[m["id"]]).model_dump())
                  for m in machines if m["id"] in by_machine],
    )


@router.post("", response_model=ProductionRecord)
async def log_production(body: ProductionLog, request: Request,
                         cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    machine = await db.machines.find_one({"company_id": cid, "id": body.machine_id})
    if not machine:
        raise HTTPException(404, "machine not found")
    require_permission(user, "production.department.write", machine["department"])
    if body.rejected > body.actual:
        raise HTTPException(422, "rejected cannot exceed actual output")
    key = {"company_id": cid, "date": body.date, "machine_id": body.machine_id}
    existing = await db.production.find_one(key)
    rec = ProductionRecord(**({"id": existing["id"]} if existing else {}), company_id=cid,
                           department=machine["department"], **body.model_dump())

    async def persist(session):
        await db.production.update_one(key, {"$set": rec.model_dump()}, upsert=True, session=session)
        return rec

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="production.recorded",
        resource_type="production_record", resource_id=rec.id, mutate=persist,
        after_state={"date": rec.date, "machine_id": rec.machine_id, "department": rec.department},
    )
