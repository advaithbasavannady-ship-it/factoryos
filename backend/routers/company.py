import os

from fastapi import APIRouter, Depends, HTTPException, Request

from lib.authz import current_user, require
from lib.audit import run_audited_mutation
from lib.permissions import require_permission
from lib.dates import today_iso
from lib.db import db
from lib.seed_data import reset_demo
from lib.tenant import get_company_id
from models.core import Company, CompanyUpdate, TodayInfo

router = APIRouter()


@router.get("/meta/today", response_model=TodayInfo)
async def get_today():
    return TodayInfo(today=today_iso())


@router.get("/company", response_model=Company)
async def get_company(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "company.read")
    doc = await db.companies.find_one({"id": cid}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "company not found")
    return Company(**doc)


@router.put("/company", response_model=Company)
async def update_company(body: CompanyUpdate, request: Request,
                         cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "company.manage")
    patch = body.model_dump(exclude_none=True)
    if "departments" in patch:
        patch["departments"] = [d.strip() for d in patch["departments"] if d.strip()]

    async def persist(session):
        if patch:
            await db.companies.update_one({"id": cid}, {"$set": patch}, session=session)
        doc = await db.companies.find_one({"id": cid}, {"_id": 0}, session=session)
        if not doc:
            raise HTTPException(404, "company not found")
        return Company(**doc)

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="company.settings_updated",
        resource_type="company", resource_id=cid, mutate=persist,
        after_state={"changed_fields": sorted(patch)},
    )


@router.post("/demo/reset", response_model=TodayInfo)
async def reset_demo_data(user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    require(user, {"owner"}, "reset demo data")
    demo_enabled = os.environ.get("ENABLE_DEMO_SEED", "false").strip().lower() == "true"
    demo_company_id = os.environ.get("DEMO_COMPANY_ID", "").strip()
    if not demo_enabled or not demo_company_id or cid != demo_company_id:
        raise HTTPException(status_code=404, detail="not found")
    await reset_demo()
    return TodayInfo(today=today_iso())
