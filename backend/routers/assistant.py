from fastapi import APIRouter, Depends, HTTPException, Response

from lib.assistant_provider import get_provider
from lib.db import db
from lib.seed_data import now_utc
from lib.tenant import get_company_id
from lib.authz import current_user, require_any_permission
from lib.permissions import has_permission, require_permission
from models.workspace import AssistantStatus, ChatMessage, Conversation, ConversationSummary, MessageIn

router = APIRouter(prefix="/assistant")


@router.get("/status", response_model=AssistantStatus)
async def status():
    p = get_provider()
    return AssistantStatus(
        connected=p.connected, provider=None if not p.connected else p.name,
        message="AI integration coming soon. Questions are saved, but no answers are generated yet."
        if not p.connected else "Connected",
    )


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_any_permission(user, "messages.company.read", "messages.department.read")
    docs = await db.conversations.find({"company_id": cid}, {"_id": 0}).sort("updated_at", -1).to_list(200)
    return [ConversationSummary(id=d["id"], title=d["title"], updated_at=d["updated_at"],
                                message_count=len(d.get("messages", []))) for d in docs]


async def _get(cid: str, conv_id: str) -> Conversation:
    doc = await db.conversations.find_one({"company_id": cid, "id": conv_id}, {"_id": 0})
    if not doc:
        raise HTTPException(404, "conversation not found")
    return Conversation(**doc)


@router.post("/conversations", response_model=Conversation, status_code=201)
async def create_conversation(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    _require_message_write(user)
    now = now_utc()
    conv = Conversation(company_id=cid, title="New conversation", created_at=now, updated_at=now)
    await db.conversations.insert_one(conv.model_dump())
    return conv


@router.get("/conversations/{conv_id}", response_model=Conversation)
async def get_conversation(conv_id: str, cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_any_permission(user, "messages.company.read", "messages.department.read")
    return await _get(cid, conv_id)


@router.post("/conversations/{conv_id}/messages", response_model=Conversation)
async def send_message(conv_id: str, body: MessageIn, cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    _require_message_write(user)
    conv = await _get(cid, conv_id)
    provider = get_provider()
    user_msg = ChatMessage(role="user", content=body.content.strip(), created_at=now_utc())
    r = await provider.reply(cid, [m.model_dump() for m in conv.messages], user_msg.content)
    bot_msg = ChatMessage(role="assistant", content=r.content, created_at=now_utc(), status=r.status,
                          link=r.link, link_label=r.link_label)
    patch: dict = {"updated_at": bot_msg.created_at}
    if not conv.messages:
        patch["title"] = user_msg.content[:60]
    await db.conversations.update_one(
        {"company_id": cid, "id": conv_id},
        {"$push": {"messages": {"$each": [user_msg.model_dump(), bot_msg.model_dump()]}}, "$set": patch},
    )
    return await _get(cid, conv_id)


@router.delete("/conversations/{conv_id}", status_code=204)
async def delete_conversation(conv_id: str, cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    _require_message_write(user)
    res = await db.conversations.delete_one({"company_id": cid, "id": conv_id})
    if not res.deleted_count:
        raise HTTPException(404, "conversation not found")
    return Response(status_code=204)


def _require_message_write(user: dict) -> None:
    if has_permission(user, "messages.company.read"):
        return
    require_permission(user, "messages.department.write")
