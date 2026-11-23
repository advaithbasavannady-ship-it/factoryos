from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from lib.analytics import group_by, production_records, sum_records
from lib.audit import run_audited_mutation
from lib.dates import today_iso
from lib.db import db
from lib.tenant import get_company_id
from lib.authz import current_user, require_permission
from models.core import (
    Machine, MachineDetail, MachineUpdate, MaintenanceCreate, MaintenanceRecord, ProductionPoint,
)

router = APIRouter()


@router.get("/machines", response_model=list[Machine])
async def list_machines(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "production.company.read")
    docs = await db.machines.find({"company_id": cid}, {"_id": 0}).sort("code", 1).to_list(1000)
    return [Machine(**d) for d in docs]


@router.get("/machines/{machine_id}", response_model=MachineDetail)
async def machine_detail(machine_id: str, cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "production.company.read")
    m = await db.machines.find_one({"company_id": cid, "id": machine_id}, {"_id": 0})
    if not m:
        raise HTTPException(404, "machine not found")
    maint = await db.maintenance.find({"company_id": cid, "machine_id": machine_id}, {"_id": 0}).sort("date", -1).to_list(200)
    today = date.fromisoformat(today_iso())
    recs = [r for r in await production_records(cid, (today - timedelta(days=13)).isoformat(), today.isoformat())
            if r["machine_id"] == machine_id]
    pts = [ProductionPoint(label=d, **sum_records(v).model_dump()) for d, v in sorted(group_by(recs, lambda r: r["date"]).items())]
    return MachineDetail(machine=Machine(**m), maintenance=[MaintenanceRecord(**x) for x in maint], production=pts)


@router.patch("/machines/{machine_id}", response_model=Machine)
async def update_machine(machine_id: str, body: MachineUpdate, request: Request,
                         cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "machines.manage")
    patch: dict = {"status": body.status}
    if body.status == "maintenance":
        patch["last_maintenance"] = today_iso()

    async def persist(session):
        res = await db.machines.update_one({"company_id": cid, "id": machine_id},
                                           {"$set": patch}, session=session)
        if not res.matched_count:
            raise HTTPException(404, "machine not found")
        return Machine(**await db.machines.find_one(
            {"company_id": cid, "id": machine_id}, {"_id": 0}, session=session))

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="machine.updated",
        resource_type="machine", resource_id=machine_id, mutate=persist,
        after_state={"status": body.status},
    )


@router.get("/maintenance", response_model=list[MaintenanceRecord])
async def list_maintenance(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "production.company.read")
    docs = await db.maintenance.find({"company_id": cid}, {"_id": 0}).sort("date", -1).to_list(2000)
    return [MaintenanceRecord(**d) for d in docs]


@router.post("/maintenance", response_model=MaintenanceRecord, status_code=201)
async def create_maintenance(body: MaintenanceCreate, request: Request,
                             cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "machines.manage")
    rec = MaintenanceRecord(company_id=cid, **body.model_dump())

    async def persist(session):
        if not await db.machines.find_one({"company_id": cid, "id": body.machine_id}, {"_id": 1}, session=session):
            raise HTTPException(404, "machine not found")
        await db.maintenance.insert_one(rec.model_dump(), session=session)
        if rec.status == "scheduled":
            await db.machines.update_one({"company_id": cid, "id": rec.machine_id},
                                         {"$set": {"next_maintenance": rec.date}}, session=session)
        elif rec.status == "completed":
            await db.machines.update_one({"company_id": cid, "id": rec.machine_id},
                                         {"$set": {"last_maintenance": rec.date}}, session=session)
        return rec

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="maintenance.recorded",
        resource_type="maintenance", resource_id=rec.id, mutate=persist,
        after_state={"machine_id": rec.machine_id, "date": rec.date, "status": rec.status},
    )
