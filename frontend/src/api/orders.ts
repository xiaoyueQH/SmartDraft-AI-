import type { Payment, Refund, ReturnCase, OrderEvent } from "./workflow";

export const orderStatuses = {
  pending_payment: "待付款",
  paid: "待发货",
  shipped: "运输中",
  delivered: "已签收",
  cancelled: "已取消",
};
export type OrderStatus = keyof typeof orderStatuses;
export interface Product {
  id: string;
  sku: string;
  name: string;
  description: string;
  price_cents: number;
  stock: number;
  is_demo: boolean;
  is_active: boolean;
  deleted_at: string | null;
  version: number;
}

export const productText = (value: string) =>
  value
    .replace(/（模拟）/g, "")
    .replace(/虚构品牌与参数，仅用于业务演示，不代表真实在售商品。/g, "")
    .replace(/；演示商品。/g, "。");
export interface OrderSummary {
  id: string;
  number: string;
  customer_username: string;
  status: OrderStatus;
  total_cents: number;
  created_at: string;
  is_demo: boolean;
}
export interface OrderDetail extends OrderSummary {
  payment?: Payment | null;
  refund?: Refund | null;
  after_sale?: ReturnCase | null;
  events?: OrderEvent[];
  cancellation?: CancellationReceipt | null;
  items: {
    id: string;
    product_id: string;
    sku: string;
    product_name: string;
    unit_price_cents: number;
    quantity: number;
  }[];
  shipments: {
    id: string;
    carrier: string;
    tracking_number: string;
    is_demo: boolean;
    events: { id: string; occurred_at: string; description: string }[];
  }[];
}
export interface ReleaseItem {
  product_id: string;
  product_name: string;
  quantity: number;
}
export interface CancellationPreview {
  confirmation_id: string;
  order_id: string;
  number: string;
  from_status: OrderStatus;
  reason: string;
  total_cents: number;
  expires_at: string;
  release_items: ReleaseItem[];
  refund_status: "not_required" | "simulated";
  refund_cents: number;
  is_demo: boolean;
}
export interface CancellationReceipt {
  confirmation_id: string;
  order_id: string;
  actor_username: string;
  actor_role: string;
  from_status: OrderStatus;
  reason: string;
  completed_at: string;
  released_units: number;
  released_items: ReleaseItem[];
  refund_status: "not_required" | "simulated";
  refund_cents: number;
  is_demo: boolean;
}
export interface Page<T> {
  items: T[];
  has_more: boolean;
}
export const money = (cents: number) =>
  new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" }).format(
    cents / 100,
  );
export const dateTime = (value: string) =>
  new Date(value).toLocaleString("zh-CN", { hour12: false });
