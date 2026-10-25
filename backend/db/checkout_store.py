import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from backend.db.identity import lock_identity
from backend.db.order_store import OrderStore
from backend.db.session import Database
from backend.models.auth import Principal
from backend.models.checkout import CheckoutRequest, CheckoutResult
from backend.models.commerce import InventoryReservation, Order, OrderItem, Product
from backend.models.commerce_errors import CommerceError
from backend.models.fulfilment import OrderEvent


class CheckoutStore:
    def __init__(self, database: Database):
        self.database = database

    async def create(self, principal: Principal, data: CheckoutRequest) -> CheckoutResult:
        fingerprint = hashlib.sha256(
            json.dumps(data.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()
        async with self.database.session() as session:
            actor = await lock_identity(session, principal)
            existing = await session.scalar(
                select(Order).where(
                    Order.user_id == actor.id,
                    Order.request_id == data.request_id,
                )
            )
            if existing:
                if existing.request_fingerprint != fingerprint:
                    raise CommerceError(
                        "ORDER_REQUEST_REUSED", "该下单请求已用于其他内容，请重新选择商品"
                    )
                order_id, created = existing.id, False
            else:
                product = await session.scalar(
                    select(Product).where(Product.id == data.product_id).with_for_update()
                )
                if not product or product.deleted_at or not product.is_active:
                    raise CommerceError("PRODUCT_UNAVAILABLE", "商品已下架或删除，请刷新商品目录")
                if product.price_cents != data.expected_price_cents:
                    raise CommerceError("PRICE_CHANGED", "商品价格已变更，请刷新后重新确认")
                if product.stock < data.quantity:
                    raise CommerceError("INSUFFICIENT_STOCK", "库存不足，请减少数量或刷新目录")
                now = datetime.now(UTC)
                if product.price_cents * data.quantity > 2_000_000_000:
                    raise CommerceError("ORDER_TOTAL_TOO_LARGE", "单笔订单金额过大，请减少数量")
                order_id, item_id = uuid4(), uuid4()
                order = Order(
                    id=order_id,
                    number=f"SF{order_id.hex.upper()}",
                    user_id=actor.id,
                    status="pending_payment",
                    total_cents=product.price_cents * data.quantity,
                    created_at=now,
                    is_demo=True,
                    request_id=data.request_id,
                    request_fingerprint=fingerprint,
                )
                session.add(order)
                await session.flush()
                session.add(
                    OrderItem(
                        id=item_id,
                        order_id=order.id,
                        product_id=product.id,
                        sku=product.sku,
                        product_name=product.name,
                        unit_price_cents=product.price_cents,
                        quantity=data.quantity,
                    )
                )
                await session.flush()
                session.add(
                    InventoryReservation(
                        order_item_id=item_id, quantity=data.quantity, created_at=now
                    )
                )
                product.stock -= data.quantity
                product.version += 1
                session.add(
                    OrderEvent(
                        order_id=order.id,
                        action="created",
                        description="订单已创建并预占库存",
                        actor_username=actor.username,
                        occurred_at=now,
                    )
                )
                await session.flush()
                created = True
        # 事务提交后才读取响应；读取中断时以原 request_id 重试可找回订单。
        detail = await OrderStore(self.database).detail(order_id, principal.account.id)
        return CheckoutResult(order=detail, created=created)
