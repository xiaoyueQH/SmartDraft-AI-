export type ItemKind =
  "specification" | "change" | "bug" | "support" | "task" | "acceptance";
export interface DiscoveryNotes {
  [key: string]: string | string[] | undefined;
  open_questions?: string[];
}
export interface Project {
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
  id: string;
  status: string;
  answer: string;
  prompt: string;
  created_at: string;
  events: AgentEvent[];
  options?: { web_search?: boolean };
}
export interface ProjectMessage {
  id: string;
  role: string;
  content: string;
  run_id?: string | null;
  created_at?: string;
}
export interface ProjectDetail {
  project: Project;
  items: ProjectItem[];
  has_more_items: boolean;
  baseline: ProjectItem | null;
  messages: ProjectMessage[];
  runs: AgentRun[];
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
