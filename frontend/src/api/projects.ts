export type ItemKind =
  "specification" | "change" | "bug" | "support" | "task" | "acceptance";
export interface DiscoveryNotes {
  [key: string]: string | string[] | undefined;
  open_questions?: string[];
}
export interface Project {
  conversation_mode?: "ai" | "pending" | "human";
  support_id?: string | null;
  conversation_version?: number;
  id: string;
  owner_id: string;
  title: string;
  brief: string;
  stack: string;
  revision: number;
  phase: "discovery" | "project";
  discovery: DiscoveryNotes;
  consultation?: {
    status: "waiting_user" | "waiting_search" | "ready" | "draft_ready";
    pending_search_query?: string;
    draft_consent: boolean;
    draft_paused?: boolean;
    memory_pending?: boolean;
    document_pending?: boolean;
    gaps: { key: string; question: string; critical: boolean }[];
    conflicts: { key: string; question: string; critical: boolean }[];
    references: {
      id: string;
      title: string;
      url: string;
      snippet: string;
      fetched_at: string;
    }[];
    suggestions: {
      id: string;
      text: string;
      reference_id: string;
      status: "pending" | "accepted" | "rejected";
    }[];
  };
  version: number;
  deleted_at: string | null;
  deleted_by: string | null;
  created_at: string;
}
export interface ProjectItem {
  content_loaded?: boolean;
  id: string;
  project_id: string;
  kind: ItemKind;
  title: string;
  content: string;
  questions: string[];
  status: string;
  base_revision: number;
  published_revision: number | null;
  customer_confirmed: boolean;
  staff_confirmed: boolean;
  version: number;
  created_by: string;
  created_at: string;
  resolution: string;
  acceptance_note: string;
}
export interface AgentEvent {
  sequence: number;
  type: string;
  node?: string;
  summary: string;
  tool?: string;
  ok?: boolean;
  duration_ms?: number;
  tokens?: { total_tokens: number };
}
export interface AgentRun {
  actor_id?: string;
  cache_version?: number;
  details_loaded?: boolean;
  id: string;
  status: string;
  answer: string;
  prompt: string;
  created_at: string;
  events: AgentEvent[];
  options?: { web_search?: boolean };
  preview?: { text: string; kind: "reply" | "document"; title: string } | null;
}
export interface ProjectMessage {
  sequence?: number | null;
  sender_id?: string | null;
  sender_type?: "customer" | "support" | "ai" | "system" | "legacy";
  sender_name?: string;
  id: string;
  role: string;
  content: string;
  run_id?: string | null;
  created_at?: string;
}
export interface ConversationStatus {
  mode: "ai" | "pending" | "human";
  version: number;
  support_id: string | null;
  support_name: string;
  last_sequence: number;
  facts_cursor: number;
  extraction_pending: boolean;
  extraction_error: string;
  human_only?: boolean;
  support_notice?: string;
  deadline_at?: string | null;
  extended_until?: string | null;
}
export interface ConversationSnapshot {
  conversation: ConversationStatus;
  messages: ProjectMessage[];
  run: AgentRun | null;
  project_version: number;
  discovery: DiscoveryNotes;
  memory_pending: boolean;
}

export const supportReasonLabels = {
  waiting: "等待接待",
  handover: "等待本轮AI结束",
  unanswered: "客户留言未回复",
  human_only: "只等人工",
  failed: "执行异常",
  expiring: "即将到期",
} as const;
export type SupportReason = keyof typeof supportReasonLabels;
export interface SupportRequest {
  id: string;
  title: string;
  customer_name: string;
  conversation_mode: "ai" | "pending" | "human";
  support_id: string | null;
  support_name: string;
  support_notice: string;
  human_only: boolean;
  reasons: SupportReason[];
  failure_summary: string;
  deadline_at: string | null;
  wait_since: string | null;
}
export interface SupportInbox {
  items: SupportRequest[];
  has_more: boolean;
  counts: Record<SupportReason | "total", number>;
  checked_at: string;
}
export function supportNextStep(item: SupportRequest): string {
  if (item.conversation_mode === "pending")
    return "先等待本轮处理完成，再回复客户";
  if (item.support_notice === "resume_required")
    return "联系客户重新登录，或接管后人工协助";
  if (item.reasons.includes("failed"))
    return "查看保存结果和处理过程，再决定重试或人工协助";
  if (item.support_id)
    return `由${item.support_name || "当前客服"}继续回复，必要时转交`;
  return item.human_only
    ? "客户只等人工，请安排客服接待"
    : "接管会话后回复客户";
}
export interface ProjectDetail {
  project: Project;
  items: ProjectItem[];
  has_more_items: boolean;
  baseline: ProjectItem | null;
  messages: ProjectMessage[];
  runs: AgentRun[];
}
export interface DocumentReview {
  id: string;
  status: "running" | "ready" | "failed" | "applied" | "discarded";
  source_version: number;
  source_content: string;
  candidate: string;
  report: {
    summary?: string;
    verified?: boolean;
    partial?: boolean;
    suggested_fixes?: number;
    unfixed_ids?: string[];
    stop_reasons?: string[];
    questions?: string[];
    blocks?: { id: string; title: string }[];
    findings?: {
      id: string;
      kind: string;
      evidence: string;
      explanation: string;
      recommendation: string;
      source_quote: string;
    }[];
    changes?: { block_id: string; old_text: string; new_text: string }[];
  };
  events: { summary: string }[];
  error: string;
}
export interface DocumentRevision {
  id: string;
  version: number;
  title: string;
  content: string;
  created_at: string;
}
export const kindLabels: Record<ItemKind, string> = {
  specification: "需求书",
  change: "需求变更",
  bug: "故障反馈",
  support: "使用咨询",
  task: "开发任务",
  acceptance: "交付验收",
};
export const stateLabels: Record<string, string> = {
  draft: "草稿",
  submitted: "待评审 / 受理",
  approved: "已发布",
  rejected: "已驳回",
  withdrawn: "已撤回",
  in_progress: "处理中",
  awaiting_acceptance: "待客户验收",
  closed: "已关闭",
};

export function itemVersionLabel(item: ProjectItem): string {
  if (item.published_revision) return `正式 V${item.published_revision}`;
  return item.base_revision
    ? `基于正式 V${item.base_revision}`
    : "初始需求 · 尚未发布";
}

export function itemStateLabel(item: ProjectItem): string {
  if (item.status === "submitted")
    return ["specification", "change"].includes(item.kind)
      ? "待双方评审"
      : "待受理";
  return stateLabels[item.status] || item.status;
}
