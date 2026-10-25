export const returnStatuses = {
  requested: "待审核",
  approved: "待寄回",
  rejected: "已驳回",
  returning: "退货中",
  received: "待退款",
  closed: "已关闭",
};
export type ReturnStatus = keyof typeof returnStatuses;
export type WorkflowAction =
  | "pay"
  | "ship"
  | "receive"
  | "return_request"
  | "return_review"
  | "return_ship"
  | "return_receive"
  | "return_refund";
export interface ReturnCase {
  id: string;
  order_id: string;
  status: ReturnStatus;
  reason: string;
  review_note: string;
  carrier: string | null;
  tracking_number: string | null;
  refund_cents: number;
  created_at: string;
  updated_at: string;
  restocked_at: string | null;
}
export interface ReturnSummary extends ReturnCase {
  number: string;
  customer_username: string;
}
export interface Payment {
  id: string;
  amount_cents: number;
  channel: string;
  paid_at: string;
}
export interface Refund {
  id: string;
  amount_cents: number;
  channel: string;
  refunded_at: string;
}
export interface OrderEvent {
  id: string;
  action: string;
  description: string;
  actor_username: string;
  occurred_at: string;
}
export interface WorkflowReceipt {
  action: WorkflowAction;
  order_id: string;
  order_status: string;
  case_id: string | null;
  case_status: ReturnStatus | null;
  occurred_at: string;
}
