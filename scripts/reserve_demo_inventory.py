import asyncio
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from backend.config import get_settings  # noqa: E402
from backend.db.session import Database, create_database  # noqa: E402
from backend.models.commerce import (  # noqa: E402
    InventoryReservation,
    Order,
    OrderCancellation,
    OrderItem,
    Product,
    Shipment,
)
from backend.models.fulfilment import OrderEvent, Payment, Refund  # noqa: E402
from backend.models.orm import User  # noqa: E402


async def reserve_demo_inventory(database: Database) -> int:
    async with database.session() as session:
        owner = await session.scalar(select(User).where(User.username == "user").with_for_update())
        if not owner or owner.role != "user" or not owner.is_active:
            raise RuntimeError("请先初始化启用的 user 演示账号")
        orders = list(
            await session.scalars(
                select(Order)
                .where(
                    Order.user_id == owner.id,
                    Order.is_demo.is_(True),
                    Order.status.in_(["pending_payment", "paid"]),
                )
                .order_by(Order.id)
                .with_for_update()
            )
        )
        missing = []
        quantities = defaultdict(int)
        for order in orders:
            if await session.scalar(
                select(Shipment.id).where(Shipment.order_id == order.id).limit(1)
            ):
                continue
            if await session.scalar(
                select(OrderCancellation.order_id).where(OrderCancellation.order_id == order.id)
            ):
                raise RuntimeError("订单状态与取消记录冲突，请核查，未修改库存")
            items = list(
                await session.scalars(select(OrderItem).where(OrderItem.order_id == order.id))
            )
            if not items:
                raise RuntimeError("演示订单缺少明细，未修改库存")
            for item in items:
                existing = await session.get(InventoryReservation, item.id)
                if existing:
                    if (
                        existing.quantity != item.quantity
                        or existing.released_at is not None
                        or existing.consumed_at is not None
                    ):
                        raise RuntimeError("演示库存预占记录异常，未修改库存")
                    continue
                missing.append(item)
                quantities[item.product_id] += item.quantity
        products = (
            list(
                await session.scalars(
                    select(Product)
                    .where(Product.id.in_(quantities))
                    .order_by(Product.id)
                    .with_for_update()
                )
            )
            if quantities
            else []
        )
        if len(products) != len(quantities) or any(
            not product.is_demo or product.stock < quantities[product.id] for product in products
        ):
            raise RuntimeError("演示商品库存不足或数据不匹配，未建立预占")
        for product in products:
            product.stock -= quantities[product.id]
            product.version += 1
        now = datetime.now(UTC)
        # 只为显式初始化的历史演示订单补账，不根据任意订单状态伪造付款。
        seeded_orders = await session.scalars(
            select(Order)
            .where(Order.user_id == owner.id, Order.is_demo.is_(True), Order.number.like("SFDEMO%"))
            .order_by(Order.id)
            .with_for_update()
        )
        for order in seeded_orders:
            cancellation = await session.scalar(
                select(OrderCancellation).where(OrderCancellation.order_id == order.id)
            )
            if order.status not in {"paid", "shipped", "delivered"} and not (
                order.status == "cancelled" and cancellation and cancellation.from_status == "paid"
            ):
                continue
            payment = await session.scalar(select(Payment).where(Payment.order_id == order.id))
            if payment and payment.amount_cents != order.total_cents:
                raise RuntimeError("历史演示订单付款金额冲突，未修改库存")
            if payment is None:
                session.add(
                    Payment(
                        order_id=order.id,
                        amount_cents=order.total_cents,
                        channel="demo_seed",
                        paid_at=order.created_at,
                    )
                )
                session.add(
                    OrderEvent(
                        order_id=order.id,
                        action="pay",
                        description="历史演示付款记录初始化",
                        actor_username=owner.username,
                        occurred_at=order.created_at,
                    )
                )
            if cancellation and cancellation.from_status == "paid":
                refund = await session.scalar(select(Refund).where(Refund.order_id == order.id))
                if cancellation.refund_cents != order.total_cents or (
                    refund and refund.amount_cents != order.total_cents
                ):
                    raise RuntimeError("历史演示订单退款金额冲突，未修改库存")
                if refund is None:
                    session.add(
                        Refund(
                            order_id=order.id,
                            amount_cents=order.total_cents,
                            channel="demo_seed",
                            refunded_at=cancellation.completed_at,
                        )
                    )
        # 历史演示库存已经是发货后的可用量，只补出库审计，不重复扣减。
        historical = await session.scalars(
            select(Order)
            .where(
                Order.user_id == owner.id,
                Order.is_demo.is_(True),
                Order.number.like("SFDEMO%"),
                Order.status.in_(["shipped", "delivered"]),
            )
            .order_by(Order.id)
            .with_for_update()
        )
        for order in historical:
            for item in await session.scalars(
                select(OrderItem).where(OrderItem.order_id == order.id)
            ):
                reservation = await session.get(InventoryReservation, item.id)
                if reservation is None:
                    session.add(
                        InventoryReservation(
                            order_item_id=item.id,
                            quantity=item.quantity,
                            created_at=order.created_at,
                            consumed_at=order.created_at,
                        )
                    )
                elif reservation.released_at is not None or reservation.quantity != item.quantity:
                    raise RuntimeError("历史演示订单的出库记录冲突，未修改库存")
                elif reservation.consumed_at is None:
                    reservation.consumed_at = order.created_at
        for item in missing:
            session.add(
                InventoryReservation(order_item_id=item.id, quantity=item.quantity, created_at=now)
            )
        return sum(quantities.values())


async def main():
    database = create_database(get_settings().database)
    try:
        quantity = await reserve_demo_inventory(database)
        print(f"本次新增预占 {quantity} 件；仅补齐可取消演示订单的预占，重复执行不重复扣减。")
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
