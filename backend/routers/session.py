from fastapi import APIRouter, Depends

from lib.authz import current_user, now_iso
from lib.db import db
from lib.tenant import get_company_id
from models.core import Notification, NotificationFeed, NotificationItem, User
from lib.permissions import require_permission

router = APIRouter()


async def notify(cid: str, roles: list[str], title: str, body: str, link: str) -> None:
    n = Notification(company_id=cid, recipient_roles=roles, title=title, body=body, link=link, created_at=now_iso())
    await db.notifications.insert_one(n.model_dump())


@router.get("/users", response_model=list[User])
async def list_users(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "users.read")
    memberships = await db.company_members.find(
        {"company_id": cid, "status": {"$in": ["active", "invited"]}}, {"_id": 0}
    ).to_list(500)
    user_ids = [membership["user_id"] for membership in memberships]
    accounts = await db.users.find({"id": {"$in": user_ids}}, {"_id": 0}).to_list(500) if user_ids else []
    by_id = {account["id"]: account for account in accounts}
    output = []
    for membership in memberships:
        account = by_id.get(membership["user_id"])
        if not account:
            continue
        role = membership.get("role", "department_employee")
        output.append(User(
            id=account["id"], company_id=cid, name=account.get("name", ""), role=role,
            title=account.get("title") or role.replace("_", " ").title(), department=membership.get("department"),
        ))
    return output


@router.get("/session", response_model=User)
async def get_session(user: dict = Depends(current_user)):
    return User(**{**user, "title": user.get("title") or str(user.get("role", "")).replace("_", " ").title()})


@router.get("/notifications", response_model=NotificationFeed)
async def notifications(user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    docs = await db.notifications.find({"company_id": cid, "recipient_roles": user["role"]}, {"_id": 0}) \
        .sort("created_at", -1).to_list(30)
    items = [NotificationItem(id=d["id"], title=d["title"], body=d["body"], link=d["link"], created_at=d["created_at"],
                              read=user["id"] in d.get("read_by", [])) for d in docs]
    return NotificationFeed(items=items, unread=sum(1 for i in items if not i.read))


@router.post("/notifications/read-all", response_model=NotificationFeed)
async def read_all(user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    await db.notifications.update_many({"company_id": cid, "recipient_roles": user["role"]}, {"$addToSet": {"read_by": user["id"]}})
    return await notifications(user, cid)
