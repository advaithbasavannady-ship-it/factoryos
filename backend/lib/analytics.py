"""Shared aggregation helpers used by dashboard, module and report routers."""

from collections import defaultdict
from datetime import date, timedelta

from lib.db import db
from models.core import AttendanceCounts, OrderSummary, ProductionTotals


def pct(a: float, b: float) -> float:
    return round(a / b * 100, 1) if b else 0.0


def totals(planned: int, actual: int, rejected: int) -> dict:
    return {
        "planned": planned, "actual": actual, "rejected": rejected,
        "achievement": pct(actual, planned),
        "efficiency": pct(actual - rejected, planned),
        "rejection_rate": pct(rejected, actual),
    }


def sum_records(records: list[dict]) -> ProductionTotals:
    p = sum(r["planned"] for r in records)
    a = sum(r["actual"] for r in records)
    rj = sum(r["rejected"] for r in records)
    return ProductionTotals(**totals(p, a, rj))


async def production_records(cid: str, start: str, end: str) -> list[dict]:
    return await db.production.find(
        {"company_id": cid, "date": {"$gte": start, "$lte": end}}, {"_id": 0}
    ).to_list(20000)


def group_by(records: list[dict], key) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        out[key(r)].append(r)
    return out


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


async def order_summary(cid: str, today: str, department_ids: list[str] | None = None) -> OrderSummary:
    query: dict = {"company_id": cid}
    if department_ids is not None:
        query["$or"] = [
            {"department": {"$in": department_ids}},
            {"stages.department": {"$in": department_ids}},
        ]
    orders = await db.orders.find(query, {"_id": 0, "department": 1, "stages.department": 1,
                                                       "status": 1, "order_date": 1, "produced": 1, "qc_passed": 1,
                                                       "qc_rejected": 1, "dispatched_date": 1, "expected_delivery": 1}).to_list(10000)
    c = defaultdict(int)
    for o in orders:
        c[o["status"]] += 1
    open_ = ("awaiting_approval", "correction_requested", "pending_review", "confirmed", "in_production", "quality_check", "ready_for_dispatch")
    live = [o for o in orders if o["status"] != "rejected"]
    return OrderSummary(
        total=len(orders), **{s: c[s] for s in ("awaiting_approval", "correction_requested", "rejected", "pending_review", "confirmed",
                                                 "in_production", "quality_check", "ready_for_dispatch", "dispatched")},
        pending=sum(c[s] for s in open_),
        overdue=sum(1 for o in orders if o["status"] in open_ and o["expected_delivery"] < today),
        received_today=sum(1 for o in orders if o["order_date"] == today),
        dispatched_today=sum(1 for o in orders if o.get("dispatched_date") == today),
        units_produced=sum(o.get("produced", 0) for o in live),
        qc_passed=sum(o.get("qc_passed", 0) for o in live),
        qc_rejected=sum(o.get("qc_rejected", 0) for o in live),
    )


def count_attendance(records: list[dict], total: int) -> AttendanceCounts:
    c = defaultdict(int)
    for r in records:
        c[r["status"]] += 1
    return AttendanceCounts(total=total, present=c["present"] + c["late"], late=c["late"],
                            absent=c["absent"], leave=c["leave"])


async def attendance_trend(cid: str, start: str, end: str) -> list[dict]:
    recs = await db.attendance.find({"company_id": cid, "date": {"$gte": start, "$lte": end}},
                                    {"_id": 0, "date": 1, "status": 1}).to_list(50000)
    by_day: dict[str, dict] = defaultdict(lambda: {"present": 0, "late": 0, "absent": 0, "leave": 0})
    for r in recs:
        by_day[r["date"]][r["status"]] += 1
    return [{"date": d, **v} for d, v in sorted(by_day.items())]
