from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from lib.audit import run_audited_mutation
from lib.db import db
from lib.tenant import get_company_id
from lib.authz import current_user, require_permission
from models.workspace import Event, EventInput

router = APIRouter(prefix="/events")

# Prototype: the Demo Owner manages the calendar. Future roles (Personal Assistant, Manager)
# will be checked here once auth lands; created_by records who made the entry.


@router.get("", response_model=list[Event])
async def list_events(start: Optional[str] = None, end: Optional[str] = None, cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "schedule.manage")
    q: dict = {"company_id": cid}
    if start or end:
        q["date"] = {k: v for k, v in (("$gte", start), ("$lte", end)) if v}
    docs = await db.events.find(q, {"_id": 0}).sort([("date", 1), ("start_time", 1)]).to_list(5000)
    return [Event(**d) for d in docs]


def _check_times(body: EventInput) -> None:
    if body.end_time <= body.start_time:
        raise HTTPException(422, "end time must be after start time")


@router.post("", response_model=Event, status_code=201)
async def create_event(body: EventInput, request: Request,
                       cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "schedule.manage")
    _check_times(body)
    ev = Event(company_id=cid, **body.model_dump())

    async def persist(session):
        await db.events.insert_one(ev.model_dump(), session=session)
        return ev

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="schedule.event_created",
        resource_type="event", resource_id=ev.id, mutate=persist,
        after_state={"date": ev.date, "start_time": ev.start_time, "end_time": ev.end_time},
    )


@router.put("/{event_id}", response_model=Event)
async def update_event(event_id: str, body: EventInput, request: Request,
                       cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "schedule.manage")
    _check_times(body)

    async def persist(session):
        res = await db.events.update_one({"company_id": cid, "id": event_id},
                                         {"$set": body.model_dump()}, session=session)
        if not res.matched_count:
            raise HTTPException(404, "event not found")
        return Event(**await db.events.find_one(
            {"company_id": cid, "id": event_id}, {"_id": 0}, session=session))

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="schedule.event_updated",
        resource_type="event", resource_id=event_id, mutate=persist,
        after_state={"date": body.date, "start_time": body.start_time, "end_time": body.end_time},
    )


@router.delete("/{event_id}", status_code=204)
async def delete_event(event_id: str, request: Request,
                       cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "schedule.manage")

    async def persist(session):
        res = await db.events.delete_one({"company_id": cid, "id": event_id}, session=session)
        if not res.deleted_count:
            raise HTTPException(404, "event not found")
        return Response(status_code=204)

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="schedule.event_deleted",
        resource_type="event", resource_id=event_id, mutate=persist,
    )
