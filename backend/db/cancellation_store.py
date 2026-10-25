import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.auth_store import utc
from backend.db.identity import lock_identity
from backend.db.session import Database
from backend.models.auth import Principal
from backend.models.cancellation import (
    CancellationError,
    CancellationPreview,
    CancellationReceipt,
    ReleaseItem,
    check_cancellable,
)
from backend.models.commerce import (
    CancellationConfirmation,
    InventoryReservation,
    Order,
    OrderCancellation,
    OrderItem,
    Product,
    Shipment,
)
from backend.models.fulfilment import OrderEvent, Payment, Refund
from backend.models.orders import OrderNotFound
from backend.models.orm import User


class CancellationStore:
    def __init__(self, database: Database):
        self.database = database

    async def _identity(self, session: AsyncSession, principal: Principal, all_users: bool) -> User:
        return await lock_identity(session, principal, admin=all_users)

    @staticmethod
    async def _order(session: AsyncSession, order_id: UUID, actor: User, all_users: bool) -> Order:
        statement = select(Order).where(Order.id == order_id)
        if not all_users:
            statement = statement.where(Order.user_id == actor.id)
        order = await session.scalar(statement.with_for_update())
        if order is None:
            raise OrderNotFound
        return order

    @staticmethod
    async def _snapshot(session: AsyncSession, order: Order):
        has_shipments = bool(
            await session.scalar(select(Shipment.id).where(Shipment.order_id == order.id).limit(1))
        )
        check_cancellable(order.status, order.is_demo, has_shipments)
        if order.status == "paid":
            payment = await session.scalar(select(Payment).where(Payment.order_id == order.id))
            if (
                payment is None
                or payment.amount_cents != order.total_cents
                or payment.channel not in {"demo", "demo_seed"}
            ):
                raise CancellationError(
                    "PAYMENT_INVALID", "付款记录缺失或金额异常，请联系管理员核查"
                )
        items = list(
            await session.scalars(
                select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
            )
        )
        if (
            not items
            or sum(item.quantity * item.unit_price_cents for item in items) != order.total_cents
        ):
            raise CancellationError("ORDER_DATA_INVALID", "订单明细或金额不完整，请联系管理员核查")
        reservations = {
            row.order_item_id: row
            for row in await session.scalars(
                select(InventoryReservation)
                .where(InventoryReservation.order_item_id.in_([item.id for item in items]))
                .with_for_update()
            )
        }
        if any(
            item.id not in reservations
            or reservations[item.id].released_at is not None
            or reservations[item.id].consumed_at is not None
            or reservations[item.id].quantity != item.quantity
            for item in items
        ):
            raise CancellationError(
                "RESERVATION_MISSING", "库存预占记录不完整，暂不能自动取消，请联系管理员"
            )
        release_items = [
            ReleaseItem(
                product_id=item.product_id, product_name=item.product_name, quantity=item.quantity
            )
            for item in items
        ]
        payload = {
            "order": [
                str(order.id),
                str(order.user_id),
                order.number,
                order.status,
                order.total_cents,
                order.is_demo,
            ],
            "items": [
                [
                    str(item.id),
                    str(item.product_id),
                    item.sku,
                    item.product_name,
                    item.unit_price_cents,
                    item.quantity,
                ]
                for item in items
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        return fingerprint, release_items, reservations

    async def preview(
        self, principal: Principal, order_id: UUID, reason: str, all_users: bool
    ) -> CancellationPreview:
        async with self.database.session() as session:
            actor = await self._identity(session, principal, all_users)
            order = await self._order(session, order_id, actor, all_users)
            fingerprint, release_items, _ = await self._snapshot(session, order)
            now = datetime.now(UTC)
            confirmation = await session.scalar(
                select(CancellationConfirmation)
                .where(
                    CancellationConfirmation.order_id == order_id,
                    CancellationConfirmation.actor_id == actor.id,
                    CancellationConfirmation.session_id == principal.session_id,
                    CancellationConfirmation.snapshot_hash == fingerprint,
                    CancellationConfirmation.reason == reason,
                    CancellationConfirmation.expires_at > now,
                )
                .order_by(CancellationConfirmation.created_at.desc())
                .limit(1)
            )
            if confirmation is None:
                confirmation = CancellationConfirmation(
                    order_id=order.id,
                    actor_id=actor.id,
                    session_id=principal.session_id,
                    snapshot_hash=fingerprint,
                    reason=reason,
                    from_status=order.status,
                    created_at=now,
                    expires_at=now + timedelta(minutes=5),
                )
                session.add(confirmation)
                await session.flush()
            return CancellationPreview(
                confirmation_id=confirmation.id,
                order_id=order.id,
                number=order.number,
                from_status=order.status,
                reason=reason,
                total_cents=order.total_cents,
                expires_at=confirmation.expires_at,
                release_items=release_items,
                refund_status="simulated" if order.status == "paid" else "not_required",
                refund_cents=order.total_cents if order.status == "paid" else 0,
                is_demo=order.is_demo,
            )

    async def confirm(
        self, principal: Principal, order_id: UUID, confirmation_id: UUID, all_users: bool
    ) -> CancellationReceipt:
        async with self.database.session() as session:
            actor = await self._identity(session, principal, all_users)
            order = await self._order(session, order_id, actor, all_users)
            confirmation = await session.scalar(
                select(CancellationConfirmation)
                .where(
                    CancellationConfirmation.id == confirmation_id,
                    CancellationConfirmation.order_id == order.id,
                    CancellationConfirmation.actor_id == actor.id,
                    CancellationConfirmation.session_id == principal.session_id,
                )
                .with_for_update()
            )
            if confirmation is None:
                raise CancellationError("CONFIRMATION_INVALID", "取消确认无效，请重新预览")
            existing = await session.get(OrderCancellation, confirmation_id)
            if existing:
                return CancellationReceipt.model_validate(existing)
            if utc(confirmation.expires_at) <= datetime.now(UTC):
                raise CancellationError("CONFIRMATION_EXPIRED", "取消确认已过期，请重新预览")
            fingerprint, release_items, reservations = await self._snapshot(session, order)
            if fingerprint != confirmation.snapshot_hash:
                raise CancellationError("ORDER_CHANGED", "订单已变化，请重新预览并确认")
            quantities = {}
            for item in release_items:
                quantities[item.product_id] = quantities.get(item.product_id, 0) + item.quantity
            products = list(
                await session.scalars(
                    select(Product)
                    .where(Product.id.in_(quantities))
                    .order_by(Product.id)
                    .with_for_update()
                )
            )
            if len(products) != len(quantities):
                raise CancellationError("INVENTORY_INVALID", "商品库存记录异常，请联系管理员核查")
            now = datetime.now(UTC)
            receipt = OrderCancellation(
                confirmation_id=confirmation.id,
                order_id=order.id,
                actor_id=actor.id,
                actor_username=actor.username,
                actor_role=actor.role,
                from_status=order.status,
                reason=confirmation.reason,
                completed_at=now,
                released_units=sum(quantities.values()),
                released_items=[item.model_dump(mode="json") for item in release_items],
                refund_status="simulated" if order.status == "paid" else "not_required",
                refund_cents=order.total_cents if order.status == "paid" else 0,
                is_demo=order.is_demo,
            )
            session.add(receipt)
            if order.status == "paid":
                session.add(
                    Refund(
                        order_id=order.id,
                        amount_cents=order.total_cents,
                        channel="demo",
                        refunded_at=now,
                    )
                )
            session.add(
                OrderEvent(
                    order_id=order.id,
                    action="cancelled",
                    description="订单已取消并释放预占库存",
                    actor_username=actor.username,
                    occurred_at=now,
                )
            )
            for product in products:
                product.stock += quantities[product.id]
                product.version += 1
            for reservation in reservations.values():
                reservation.released_at = now
            order.status = "cancelled"
            await session.flush()
            return CancellationReceipt.model_validate(receipt)
