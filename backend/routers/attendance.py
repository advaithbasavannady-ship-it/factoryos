from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from lib.analytics import attendance_trend, count_attendance
from lib.audit import run_audited_mutation
from lib.dates import today_iso
from lib.db import db
from lib.tenant import get_company_id
from lib.authz import current_user, require_any_permission, require_permission
from lib.permissions import has_permission
from models.core import (
    AttendanceDay, AttendanceRecord, AttendanceRow, AttendanceTrendPoint, AttendanceUpdate,
    DeptAttendance, Employee, EmployeeHistory,
)

router = APIRouter()


def _employee_scope(user: dict) -> dict:
    if has_permission(user, "attendance.company.read"):
        return {}
    departments = user.get("department_ids") or ([user.get("department")] if user.get("department") else [])
    return {"department": {"$in": departments}}


@router.get("/employees", response_model=list[Employee])
async def list_employees(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_any_permission(user, "attendance.company.read", "attendance.department.read")
    docs = await db.employees.find({"company_id": cid, **_employee_scope(user)}, {"_id": 0}).sort("emp_code", 1).to_list(5000)
    return [Employee(**d) for d in docs]


@router.get("/attendance/day", response_model=AttendanceDay)
async def attendance_day(date: Optional[str] = None, cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_any_permission(user, "attendance.company.read", "attendance.department.read")
    day = date or today_iso()
    emps = await db.employees.find({"company_id": cid, **_employee_scope(user)}, {"_id": 0}).sort("emp_code", 1).to_list(5000)
    emp_ids = [e["id"] for e in emps]
    recs = await db.attendance.find({"company_id": cid, "date": day, "employee_id": {"$in": emp_ids}}, {"_id": 0}).to_list(5000)
    by_emp = {r["employee_id"]: r for r in recs}
    dept_emps: dict[str, int] = defaultdict(int)
    dept_recs: dict[str, list[dict]] = defaultdict(list)
    for e in emps:
        dept_emps[e["department"]] += 1
        if e["id"] in by_emp:
            dept_recs[e["department"]].append(by_emp[e["id"]])
    depts = [DeptAttendance(department=d, **count_attendance(dept_recs[d], n).model_dump()) for d, n in dept_emps.items()]
    return AttendanceDay(
        date=day, summary=count_attendance(recs, len(emps)), departments=depts,
        rows=[AttendanceRow(employee=Employee(**e), record=AttendanceRecord(**by_emp[e["id"]]) if e["id"] in by_emp else None)
              for e in emps],
    )


@router.put("/attendance/{employee_id}", response_model=AttendanceRecord)
async def upsert_attendance(employee_id: str, body: AttendanceUpdate, request: Request,
                            cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    employee = await db.employees.find_one({"company_id": cid, "id": employee_id}, {"_id": 0, "department": 1})
    if not employee:
        raise HTTPException(404, "employee not found")
    require_permission(user, "attendance.department.write", (employee or {}).get("department"))
    data = body.model_dump()
    if data["status"] in ("absent", "leave"):
        data["check_in"] = data["check_out"] = None

    async def persist(session):
        existing = await db.attendance.find_one(
            {"company_id": cid, "employee_id": employee_id, "date": body.date}, {"_id": 0}, session=session,
        )
        rec = AttendanceRecord(
            id=existing["id"] if existing else AttendanceRecord.model_fields["id"].default_factory(),
            company_id=cid, employee_id=employee_id, **data,
        )
        await db.attendance.update_one(
            {"company_id": cid, "employee_id": employee_id, "date": body.date},
            {"$set": rec.model_dump()}, upsert=True, session=session,
        )
        return rec

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="attendance.recorded",
        resource_type="attendance", resource_id=f"{employee_id}:{body.date}", mutate=persist,
        after_state={"employee_id": employee_id, "date": body.date, "status": data["status"]},
    )


@router.get("/attendance/trend", response_model=list[AttendanceTrendPoint])
async def get_attendance_trend(days: int = 30, cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_any_permission(user, "attendance.company.read", "attendance.department.read")
    end = date.fromisoformat(today_iso())
    start = end - timedelta(days=max(1, min(days, 90)) - 1)
    if has_permission(user, "attendance.company.read"):
        records = await attendance_trend(cid, start.isoformat(), end.isoformat())
    else:
        departments = user.get("department_ids") or ([user.get("department")] if user.get("department") else [])
        emps = await db.employees.find({"company_id": cid, "department": {"$in": departments}},
                                      {"_id": 0, "id": 1}).to_list(5000)
        ids = [e["id"] for e in emps]
        recs = await db.attendance.find({"company_id": cid, "employee_id": {"$in": ids},
                                         "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}},
                                        {"_id": 0, "date": 1, "status": 1}).to_list(50000)
        daily: dict[str, dict] = defaultdict(lambda: {"present": 0, "late": 0, "absent": 0, "leave": 0})
        for rec in recs:
            daily[rec["date"]][rec["status"]] += 1
        records = [{"date": d, **v} for d, v in sorted(daily.items())]
    return [AttendanceTrendPoint(**p) for p in records]


@router.get("/employees/{employee_id}/history", response_model=EmployeeHistory)
async def employee_history(employee_id: str, days: int = 30, cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    emp = await db.employees.find_one({"company_id": cid, "id": employee_id}, {"_id": 0})
    if not emp:
        raise HTTPException(404, "employee not found")
    if not has_permission(user, "attendance.company.read") and emp.get("department") not in _employee_scope(user).get("department", {}).get("$in", []):
        raise HTTPException(status_code=404, detail="employee not found")
    start = (date.fromisoformat(today_iso()) - timedelta(days=days - 1)).isoformat()
    recs = await db.attendance.find({"company_id": cid, "employee_id": employee_id, "date": {"$gte": start}},
                                    {"_id": 0}).sort("date", -1).to_list(500)
    return EmployeeHistory(employee=Employee(**emp), records=[AttendanceRecord(**r) for r in recs],
                           summary=count_attendance(recs, len(recs)))
