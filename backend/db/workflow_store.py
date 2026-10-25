import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.identity import lock_identity
from backend.db.session import Database
from backend.models.auth import Principal
from backend.models.commerce import (
    InventoryReservation,
    Order,
    OrderItem,
    Product,
    Shipment,
    ShipmentEvent,
)
from backend.models.commerce_errors import CommerceError
from backend.models.fulfilment import OrderEvent, Payment, Refund, ReturnCase, WorkflowAction
from backend.models.orders import OrderNotFound
from backend.models.orm import User
from backend.models.workflow import (
    ADMIN_ACTIONS,
    Action,
    Confirmation,
    ReturnCommand,
    ReturnPage,
    ReturnStatus,
    ReturnSummary,
    ReturnView,
    ReviewCommand,
    ShipmentCommand,
    WorkflowReceipt,
)


class WorkflowStore:
    def __init__(self, database: Database):
        self.database = database

    async def returns(
        self, owner_id: UUID | None, status: ReturnStatus | None, limit: int, offset: int
    ) -> ReturnPage:
        statement = (
            select(ReturnCase, Order.number, User.username)
            .join(Order, Order.id == ReturnCase.order_id)
            .join(User, User.id == Order.user_id)
        )
        if owner_id is not None:
            statement = statement.where(Order.user_id == owner_id)
        if status:
            statement = statement.where(ReturnCase.status == status)
        async with self.database.session() as session:
            rows = (
                await session.execute(
                    statement.order_by(ReturnCase.created_at.desc(), ReturnCase.id)
                    .limit(limit + 1)
                    .offset(offset)
                )
            ).all()
            return ReturnPage(
                items=[
                    ReturnSummary(
                        **ReturnView.model_validate(case).model_dump(),
                        number=number,
                        customer_username=username,
                    )
                    for case, number, username in rows[:limit]
                ],
                has_more=len(rows) > limit,
            )

    @staticmethod
    def require_state(actual: str, expected: str):
        if actual != expected:
            raise CommerceError("WORKFLOW_CONFLICT", "状态已变化或不支持此操作，请刷新后核对")

    @staticmethod
    async def inventory(session: AsyncSession, order: Order, *, consumed: bool):
        items = list(
            await session.scalars(
                select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id)
            )
        )
        if (
            not items
            or order.total_cents <= 0
            or sum(i.unit_price_cents * i.quantity for i in items) != order.total_cents
        ):
            raise CommerceError("ORDER_DATA_INVALID", "订单明细或金额异常，请联系管理员")
        reservations = {
            r.order_item_id: r
            for r in await session.scalars(
                select(InventoryReservation)
                .where(InventoryReservation.order_item_id.in_([i.id for i in items]))
                .order_by(InventoryReservation.order_item_id)
                .with_for_update()
            )
        }
        if any(
            i.id not in reservations
            or reservations[i.id].quantity != i.quantity
            or reservations[i.id].released_at is not None
            or (reservations[i.id].consumed_at is not None) != consumed
            for i in items
        ):
            raise CommerceError("INVENTORY_INVALID", "库存预占或出库记录不完整，暂不能继续操作")
        quantities = defaultdict(int)
        for item in items:
            quantities[item.product_id] += item.quantity
        return reservations, quantities

    async def run(
        self, principal: Principal, order_id: UUID, action: Action, command: Confirmation
    ) -> WorkflowReceipt:
        admin = action in ADMIN_ACTIONS
        fingerprint = hashlib.sha256(
            json.dumps(
                {"action": action, **command.model_dump(mode="json")}, sort_keys=True
            ).encode()
        ).hexdigest()
        action_id = uuid5(
            NAMESPACE_URL,
            f"supportflow:workflow:{principal.account.id}:{order_id}:{command.operation_id}",
        )
        async with self.database.session() as session:
            actor = await lock_identity(session, principal, admin=admin)
            statement = select(Order).where(Order.id == order_id)
            if not admin:
                statement = statement.where(Order.user_id == actor.id)
            order = await session.scalar(statement.with_for_update())
            if not order:
                raise OrderNotFound
            previous = await session.get(WorkflowAction, action_id)
            if previous:
                if previous.fingerprint != fingerprint:
                    raise CommerceError(
                        "OPERATION_REUSED", "此操作编号已用于其他内容，请重新打开订单"
                    )
                return WorkflowReceipt.model_validate(previous.result_snapshot)
            if not order.is_demo:
                raise CommerceError("PROVIDER_UNAVAILABLE", "当前未接入真实付款、发货或退款渠道")
            if action != "pay":
                payment = await session.scalar(select(Payment).where(Payment.order_id == order.id))
                if (
                    payment is None
                    or payment.amount_cents != order.total_cents
                    or payment.channel not in {"demo", "demo_seed"}
                ):
                    raise CommerceError(
                        "PAYMENT_INVALID", "付款记录缺失或金额异常，请联系管理员核查"
                    )
            case = await session.scalar(
                select(ReturnCase).where(ReturnCase.order_id == order.id).with_for_update()
            )
            now = datetime.now(UTC)
            label, case = await self.transition(session, order, case, action, command, now)
            session.add(
                OrderEvent(
                    order_id=order.id,
                    action=action,
                    description=label,
                    actor_username=actor.username,
                    occurred_at=now,
                )
            )
            if case:
                case.updated_at = now
            await session.flush()
            receipt = WorkflowReceipt(
                action=action,
                order_id=order.id,
                order_status=order.status,
                case_id=case.id if case else None,
                case_status=case.status if case else None,
                occurred_at=now,
            )
            session.add(
                WorkflowAction(
                    id=action_id,
                    order_id=order.id,
                    actor_id=actor.id,
                    fingerprint=fingerprint,
                    result_snapshot=receipt.model_dump(mode="json"),
                )
            )
            await session.flush()
            return receipt

    async def transition(
        self,
        session: AsyncSession,
        order: Order,
        case: ReturnCase | None,
        action: Action,
        command: Confirmation,
        now: datetime,
    ):
        if action == "pay":
            self.require_state(order.status, "pending_payment")
            await self.inventory(session, order, consumed=False)
            if await session.scalar(select(Payment.id).where(Payment.order_id == order.id)):
                raise CommerceError("PAYMENT_EXISTS", "付款记录已存在，请刷新订单")
            session.add(
                Payment(
                    order_id=order.id, amount_cents=order.total_cents, channel="demo", paid_at=now
                )
            )
            order.status = "paid"
            return "演示付款已确认，等待商家发货", case
        if action == "ship" and isinstance(command, ShipmentCommand):
            self.require_state(order.status, "paid")
            if await session.scalar(
                select(Shipment.id).where(Shipment.order_id == order.id).limit(1)
            ):
                raise CommerceError("SHIPMENT_EXISTS", "订单已存在发货记录，请刷新")
            reservations, _ = await self.inventory(session, order, consumed=False)
            shipment = Shipment(
                order_id=order.id,
                carrier=command.carrier,
                tracking_number=command.tracking_number,
                is_demo=True,
            )
            session.add(shipment)
            await session.flush()
            session.add(
                ShipmentEvent(
                    shipment_id=shipment.id,
                    occurred_at=now,
                    description="商家已登记发货（演示运单）",
                )
            )
            for reservation in reservations.values():
                reservation.consumed_at = now
            order.status = "shipped"
            return "商家已发货，库存预占转为出库", case
        if action == "receive":
            self.require_state(order.status, "shipped")
            await self.inventory(session, order, consumed=True)
            shipments = list(
                await session.scalars(select(Shipment).where(Shipment.order_id == order.id))
            )
            if not shipments:
                raise CommerceError("SHIPMENT_MISSING", "订单缺少发货记录")
            for shipment in shipments:
                session.add(
                    ShipmentEvent(
                        shipment_id=shipment.id, occurred_at=now, description="用户已确认收货"
                    )
                )
            order.status = "delivered"
            return "用户确认收货，订单履约完成", case
        self.require_state(order.status, "delivered")
        if action == "return_request" and isinstance(command, ReturnCommand):
            if case:
                raise CommerceError("RETURN_EXISTS", "此订单已有售后工单，请查看原工单")
            await self.inventory(session, order, consumed=True)
            case = ReturnCase(
                order_id=order.id,
                status="requested",
                reason=command.reason,
                review_note="",
                refund_cents=order.total_cents,
                created_at=now,
                updated_at=now,
            )
            session.add(case)
            return "用户申请整单退货退款，等待审核", case
        if not case:
            raise CommerceError("RETURN_NOT_FOUND", "此订单没有售后工单", 404)
        if action == "return_review" and isinstance(command, ReviewCommand):
            self.require_state(case.status, "requested")
            case.review_note = command.reason
            case.status = "approved" if command.decision == "approve" else "rejected"
            return (
                "管理员已同意退货，请用户登记退货运单"
                if case.status == "approved"
                else "管理员已驳回申请并记录原因",
                case,
            )
        if action == "return_ship" and isinstance(command, ShipmentCommand):
            self.require_state(case.status, "approved")
            case.carrier, case.tracking_number, case.status = (
                command.carrier,
                command.tracking_number,
                "returning",
            )
            return "用户已登记退货运单，等待商家验收", case
        if action == "return_receive":
            self.require_state(case.status, "returning")
            if case.restocked_at:
                raise CommerceError("ALREADY_RESTOCKED", "退货已验收，请刷新工单")
            _, quantities = await self.inventory(session, order, consumed=True)
            products = list(
                await session.scalars(
                    select(Product)
                    .where(Product.id.in_(quantities))
                    .order_by(Product.id)
                    .with_for_update()
                )
            )
            if len(products) != len(quantities):
                raise CommerceError("INVENTORY_INVALID", "商品库存记录不完整")
            for product in products:
                product.stock += quantities[product.id]
                product.version += 1
            case.restocked_at, case.status = now, "received"
            return "商家已验收退货，库存已补回，等待退款", case
        if action == "return_refund":
            self.require_state(case.status, "received")
            if not case.restocked_at or case.refund_cents != order.total_cents:
                raise CommerceError("REFUND_DATA_INVALID", "验收或退款金额异常")
            if await session.scalar(select(Refund.id).where(Refund.order_id == order.id)):
                raise CommerceError("REFUND_EXISTS", "退款记录已存在")
            session.add(
                Refund(
                    order_id=order.id,
                    case_id=case.id,
                    amount_cents=order.total_cents,
                    channel="demo",
                    refunded_at=now,
                )
            )
            case.status = "closed"
            return "演示退款已登记，售后工单已关闭", case
        raise CommerceError("ACTION_INVALID", "不支持的业务操作", 422)
