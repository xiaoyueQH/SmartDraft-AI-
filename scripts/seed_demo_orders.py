import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert  # noqa: E402

from backend.config import get_settings  # noqa: E402
from backend.db.session import Database, create_database  # noqa: E402
from backend.models.commerce import Order, OrderItem, Product, Shipment, ShipmentEvent  # noqa: E402
from backend.models.orm import User  # noqa: E402
from scripts.reserve_demo_inventory import reserve_demo_inventory  # noqa: E402

PRODUCTS = (
    ("DEMO-KETTLE", "恒温电热水壶", "1.5L 容量，支持多档温控；演示商品。", 12900, 80),
    ("DEMO-HEADSET", "无线降噪耳机", "蓝牙连接，支持主动降噪；演示商品。", 29900, 50),
    ("DEMO-LAMP", "护眼阅读台灯", "三档色温，无级调光；演示商品。", 8900, 120),
)
STATUSES = ("pending_payment", "paid", "shipped", "delivered", "cancelled")


def demo_id(key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"supportflow.demo.{key}")


async def seed_orders(database: Database) -> int:
    created = 0
    now = datetime.now(UTC).replace(microsecond=0)
    async with database.session() as session:
        owner = await session.scalar(select(User).where(User.username == "user").with_for_update())
        if owner is None or not owner.is_active or owner.role != "user":
            raise RuntimeError("请先运行 scripts/seed_demo_users.py，确保 user 为启用的普通用户")
        for sku, name, description, price, stock in PRODUCTS:
            await session.execute(
                insert(Product)
                .values(
                    id=demo_id(sku),
                    sku=sku,
                    name=name,
                    description=description,
                    price_cents=price,
                    stock=stock,
                    is_demo=True,
                )
                .on_conflict_do_nothing(index_elements=[Product.id])
            )
        for index, status in enumerate(STATUSES, 1):
            order_id = demo_id(f"order.{owner.id}.{index}")
            if await session.get(Order, order_id):
                continue
            sku, name, _, price, _ = PRODUCTS[(index - 1) % len(PRODUCTS)]
            quantity = 2 if index == 2 else 1
            created_at = now - timedelta(days=index + 1)
            session.add(
                Order(
                    id=order_id,
                    number=f"SFDEMO{owner.id.hex[:8].upper()}{index:03d}",
                    user_id=owner.id,
                    status=status,
                    total_cents=price * quantity,
                    created_at=created_at,
                    is_demo=True,
                )
            )
            await session.flush()
            session.add(
                OrderItem(
                    id=demo_id(f"item.{order_id}"),
                    order_id=order_id,
                    product_id=demo_id(sku),
                    sku=sku,
                    product_name=name,
                    unit_price_cents=price,
                    quantity=quantity,
                )
            )
            if status in {"shipped", "delivered"}:
                shipment_id = demo_id(f"shipment.{order_id}")
                session.add(
                    Shipment(
                        id=shipment_id,
                        order_id=order_id,
                        carrier="演示物流（未接入真实承运商）",
                        tracking_number=f"DEMO-{index:06d}",
                        is_demo=True,
                    )
                )
                await session.flush()
                descriptions = ["仓库已打包出库（模拟）", "包裹运输中（模拟）"]
                if status == "delivered":
                    descriptions.append("包裹已签收（模拟）")
                for step, description in enumerate(descriptions, 1):
                    session.add(
                        ShipmentEvent(
                            id=demo_id(f"event.{order_id}.{step}"),
                            shipment_id=shipment_id,
                            occurred_at=created_at + timedelta(hours=step * 12),
                            description=description,
                        )
                    )
            created += 1
    return created


async def main():
    database = create_database(get_settings().database)
    try:
        count = await seed_orders(database)
        reserved = await reserve_demo_inventory(database)
        print(f"新增 {count} 笔演示订单；已有订单保持不变。商品、库存和物流均为模拟数据。")
        print(f"本次新增库存预占 {reserved} 件。")
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(main())
