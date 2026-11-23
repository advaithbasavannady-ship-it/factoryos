from collections import defaultdict
from datetime import date, timedelta
import hashlib

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pymongo import ReturnDocument

from lib.dates import today_iso
from lib.db import db
from lib.idempotency import run_idempotent_mutation
from lib.tenant import get_company_id
from lib.authz import current_user, require_permission
from models.core import (
    ConsumptionSeries, ConsumptionSeriesItem, InventoryItem, InventorySummary, Purchase, PurchaseCreate,
    PurchaseUpdate, StockAdjust, Supplier,
)

router = APIRouter()


@router.get("/inventory/items", response_model=list[InventoryItem])
async def list_items(category: str = "all", cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "inventory.read")
    q: dict = {"company_id": cid}
    if category != "all":
        q["category"] = category
    return [InventoryItem(**d) for d in await db.inventory.find(q, {"_id": 0}).sort("sku", 1).to_list(5000)]


@router.get("/inventory/summary", response_model=InventorySummary)
async def inventory_summary(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "inventory.read")
    items = await db.inventory.find({"company_id": cid}, {"_id": 0}).to_list(5000)
    return InventorySummary(
        raw_count=sum(1 for i in items if i["category"] == "raw"),
        finished_count=sum(1 for i in items if i["category"] == "finished"),
        stock_value=round(sum(i["quantity"] * i["unit_cost"] for i in items), 2),
        low_stock=sum(1 for i in items if i["quantity"] <= i["reorder_level"]),
        open_purchases=await db.purchases.count_documents({"company_id": cid, "status": "ordered"}),
    )


@router.post("/inventory/items/{item_id}/adjust", response_model=InventoryItem)
async def adjust_stock(item_id: str, body: StockAdjust, request: Request,
                       idempotency_key: str = Header(..., alias="Idempotency-Key"),
                       cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "inventory.manage")
    stock_query: dict = {"company_id": cid, "id": item_id}
    if body.delta < 0:
        stock_query["quantity"] = {"$gte": -body.delta}

    async def apply_adjustment(session):
        item = await db.inventory.find_one_and_update(
            stock_query,
            {"$inc": {"quantity": body.delta}},
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if not item:
            exists = await db.inventory.find_one(
                {"company_id": cid, "id": item_id}, {"_id": 1}, session=session,
            )
            if not exists:
                raise HTTPException(404, "item not found")
            raise HTTPException(422, "stock cannot go below zero")

        if body.delta < 0 and item["category"] == "raw":
            today = today_iso()
            consumption_id = hashlib.sha256(f"{cid}:{item_id}:{today}".encode()).hexdigest()
            await db.consumption.update_one(
                {"_id": consumption_id},
                {"$inc": {"quantity": -body.delta},
                 "$setOnInsert": {"id": consumption_id, "company_id": cid,
                                   "item_id": item_id, "date": today}},
                upsert=True,
                session=session,
            )
        return item

    item = await run_idempotent_mutation(
        company_id=cid, actor_id=user["id"], operation=f"inventory.adjust:{item_id}",
        key=idempotency_key, payload=body.model_dump(mode="json"), mutate=apply_adjustment,
        user=user, request=request, audit_action="inventory.stock_adjusted", audit_resource_type="inventory_item",
    )
    return InventoryItem(**item)


@router.get("/inventory/consumption", response_model=ConsumptionSeries)
async def consumption(days: int = 14, cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "inventory.read")
    end = date.fromisoformat(today_iso())
    dates = [(end - timedelta(days=k)).isoformat() for k in range(max(1, min(days, 31)) - 1, -1, -1)]
    items = await db.inventory.find({"company_id": cid, "category": "raw"}, {"_id": 0}).sort("sku", 1).to_list(1000)
    recs = await db.consumption.find({"company_id": cid, "date": {"$gte": dates[0]}}, {"_id": 0}).to_list(20000)
    m: dict[tuple, float] = defaultdict(float)
    for r in recs:
        m[(r["item_id"], r["date"])] += r["quantity"]
    out = []
    for i in items:
        vals = [round(m[(i["id"], d)], 1) for d in dates]
        out.append(ConsumptionSeriesItem(item_id=i["id"], name=i["name"], unit=i["unit"], total=round(sum(vals), 1), values=vals))
    return ConsumptionSeries(dates=dates, items=out)


@router.get("/suppliers", response_model=list[Supplier])
async def list_suppliers(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "purchases.read")
    return [Supplier(**d) for d in await db.suppliers.find({"company_id": cid}, {"_id": 0}).sort("name", 1).to_list(1000)]


@router.get("/purchases", response_model=list[Purchase])
async def list_purchases(cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "purchases.read")
    docs = await db.purchases.find({"company_id": cid}, {"_id": 0}).sort([("order_date", -1), ("po_no", -1)]).to_list(5000)
    return [Purchase(**d) for d in docs]


@router.post("/purchases", response_model=Purchase, status_code=201)
async def create_purchase(body: PurchaseCreate, request: Request,
                          idempotency_key: str = Header(..., alias="Idempotency-Key"),
                          cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "purchases.manage")

    async def create(session):
        item = await db.inventory.find_one({"company_id": cid, "id": body.item_id}, session=session)
        if not item:
            raise HTTPException(404, "item not found")
        supplier_id = body.supplier_id or item.get("supplier_id")
        if not supplier_id or not await db.suppliers.find_one(
                {"company_id": cid, "id": supplier_id}, session=session):
            raise HTTPException(422, "a valid supplier is required")
        count = await db.purchases.count_documents({"company_id": cid}, session=session)
        today = today_iso()
        purchase = Purchase(
            company_id=cid, po_no=f"PO-{today[:4]}-{301 + count:04d}", supplier_id=supplier_id,
            item_id=body.item_id, quantity=body.quantity,
            unit_cost=body.unit_cost if body.unit_cost is not None else item["unit_cost"],
            order_date=today, expected_date=body.expected_date, status="ordered",
        )
        await db.purchases.insert_one(purchase.model_dump(), session=session)
        return purchase

    result = await run_idempotent_mutation(
        company_id=cid, actor_id=user["id"], operation="purchase.create",
        key=idempotency_key, payload=body.model_dump(mode="json"), mutate=create,
        user=user, request=request, audit_action="purchase.created", audit_resource_type="purchase",
    )
    return Purchase(**result)


@router.patch("/purchases/{purchase_id}", response_model=Purchase)
async def update_purchase(purchase_id: str, body: PurchaseUpdate, request: Request,
                          idempotency_key: str = Header(..., alias="Idempotency-Key"),
                          cid: str = Depends(get_company_id), user: dict = Depends(current_user)):
    require_permission(user, "purchases.manage")

    async def transition_purchase(session):
        p = await db.purchases.find_one({"company_id": cid, "id": purchase_id}, {"_id": 0}, session=session)
        if not p:
            raise HTTPException(404, "purchase not found")
        if p["status"] != "ordered":
            raise HTTPException(409, f"purchase already {p['status']}")

        updated = await db.purchases.find_one_and_update(
            {"company_id": cid, "id": purchase_id, "status": "ordered"},
            {"$set": {"status": body.status}},
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if not updated:
            raise HTTPException(409, "purchase state changed; refresh and retry")
        if body.status == "received":
            await db.inventory.update_one(
                {"company_id": cid, "id": p["item_id"]},
                {"$inc": {"quantity": p["quantity"]}},
                session=session,
            )
        return updated

    updated = await run_idempotent_mutation(
        company_id=cid, actor_id=user["id"], operation=f"purchase.status:{purchase_id}",
        key=idempotency_key, payload=body.model_dump(mode="json"), mutate=transition_purchase,
        user=user, request=request, audit_action="purchase.status_changed", audit_resource_type="purchase",
    )
    return Purchase(**updated)
