<script setup lang="ts">
import {
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  reactive,
  ref,
  watch,
} from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate } from "vue-router";
import { auth, authState } from "../auth/session";
import { ApiError, type User } from "../auth/client";
import {
  kindLabels,
  itemVersionLabel,
  itemStateLabel,
  type AgentRun,
  type ItemKind,
  type Project,
  type ProjectDetail,
  type ProjectItem,
  type ConversationStatus,
  type ConversationSnapshot,
} from "../api/projects";
import RequirementDocument from "../components/RequirementDocument.vue";
import RecordManager from "../components/RecordManager.vue";
import ConversationPanel from "../components/ConversationPanel.vue";
import DocumentReview from "../components/DocumentReview.vue";
import ConversationHistory from "../components/ConversationHistory.vue";

const props = defineProps<{
  projectId?: string;
  initialTab?: string;
  initialItemId?: string;
}>();
const admin = computed(() => authState.user?.role === "admin");
const projects = ref<Project[]>([]),
  detail = ref<ProjectDetail | null>(null),
  customers = ref<User[]>([]);
const selected = ref(""),
  tab = ref("conversation"),
  notice = ref(""),
  loading = ref(false),
  starting = ref(false),
  saving = ref(false);
const projectOffset = ref(0),
  hasMoreProjects = ref(false),
  itemOffset = ref(0),
  olderItems = ref<ProjectItem[]>([]),
  hasMoreItems = ref(false);
const prompt = ref(""),
  configured = ref(false),
  run = ref<AgentRun | null>(null);
const activity = ref<
  {
    id: string;
    action: string;
    actor_id: string;
    note: string;
    created_at: string;
  }[]
>([]);
const consultationDialog = ref<HTMLDialogElement | null>(null),
  itemDialog = ref<HTMLDialogElement | null>(null);
const selectedCustomer = ref("");
const recordManager = ref<InstanceType<typeof RecordManager> | null>(null),
  recordBusy = ref(false);
const itemHistory = ref<
  { id: string; action: string; note: string; created_at: string }[]
>([]);
const comparison = ref<{
    before: string;
    after: string;
    base_revision: number;
    published_revision: number | null;
  } | null>(null),
  comparing = ref(false);
const createdToOpen = ref<Project | null>(null);
const showNotes = ref(false);
const documentBusy = ref(false);
const conversation = ref<ConversationStatus | null>(null);
const supportChoices = ref<User[]>([]),
  transferTarget = ref("");
let conversationController: AbortController | null = null;
let conversationTimer: ReturnType<typeof setTimeout> | null = null;
function stopConversation() {
  conversationController?.abort();
  conversationController = null;
  if (conversationTimer) clearTimeout(conversationTimer);
  conversationTimer = null;
}
function applyConversation(
  next: ConversationSnapshot,
  id: string,
  current: number,
) {
  if (
    disposed ||
    current !== generation ||
    selected.value !== id ||
    !detail.value
  )
    return;
  if (
    conversation.value &&
    (next.conversation.version < conversation.value.version ||
      next.conversation.last_sequence < conversation.value.last_sequence)
  )
    return;
  const previousVersion = detail.value.project.version;
  conversation.value = next.conversation;
  detail.value.messages = next.messages;
  detail.value.project.conversation_mode = next.conversation.mode;
  detail.value.project.support_id = next.conversation.support_id;
  detail.value.project.conversation_version = next.conversation.version;
  if (next.project_version >= detail.value.project.version) {
    detail.value.project.version = next.project_version;
    detail.value.project.discovery = next.discovery;
    if (detail.value.project.consultation)
      detail.value.project.consultation.memory_pending = next.memory_pending;
  }
  if (
    next.run &&
    (!run.value ||
      run.value.id !== next.run.id ||
      (next.run.cache_version || 0) >= (run.value.cache_version || 0))
  )
    run.value = next.run;
  if (
    next.project_version > previousVersion &&
    !saving.value &&
    !documentBusy.value
  )
    void refresh().catch((error) => {
      if (!disposed && current === generation) notice.value = errorText(error);
    });
}
function startConversationFeed(id: string, current: number) {
  stopConversation();
  const controller = new AbortController();
  conversationController = controller;
  async function follow() {
    try {
      if (typeof auth.stream === "function") {
        await auth.stream(
          `/api/projects/${id}/conversation/stream`,
          (event, data) => {
            if (!controller.signal.aborted && event === "conversation")
              applyConversation(data as ConversationSnapshot, id, current);
          },
          controller.signal,
        );
      } else {
        const result = await auth.request<ConversationSnapshot>(
          `/api/projects/${id}/conversation`,
        );
        if (!controller.signal.aborted) applyConversation(result, id, current);
      }
    } catch (error) {
      if (controller.signal.aborted || disposed || current !== generation)
        return;
      if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
        notice.value = errorText(error);
        return;
      }
      try {
        const result = await auth.request<ConversationSnapshot>(
          `/api/projects/${id}/conversation`,
        );
        if (!controller.signal.aborted) applyConversation(result, id, current);
      } catch {
        /* Next authorized read retries; messages are never resubmitted. */
      }
    }
    if (!controller.signal.aborted && !disposed && current === generation)
      conversationTimer = setTimeout(follow, 1500);
  }
  void follow();
}
async function controlConversation(action: string) {
  if (!conversation.value) return;
  const id = selected.value,
    current = generation;
  const result = await write(`/api/projects/${id}/conversation/actions`, {
    request_id: crypto.randomUUID(),
    expected_version: conversation.value.version,
    action,
    support_id: action === "transfer" ? transferTarget.value : undefined,
  });
  if (result && !disposed && current === generation && selected.value === id) {
    conversation.value = result.conversation as ConversationStatus;
    transferTarget.value = "";
    supportChoices.value = [];
  }
}
async function chooseSupport() {
  const current = generation;
  try {
    const users = await auth.request<User[]>("/api/admin/users?limit=100");
    if (!disposed && current === generation)
      supportChoices.value = users.filter(
        (user) =>
          user.role === "admin" &&
          user.is_active &&
          user.id !== authState.user?.id,
      );
  } catch (error) {
    if (!disposed && current === generation) notice.value = errorText(error);
  }
}

function documentChanged(item: ProjectItem) {
  if (opened.value?.id !== item.id) return;
  opened.value = item;
  Object.assign(itemForm, {
    title: item.title,
    content: item.content,
    questions: item.questions.join("\n"),
  });
  if (detail.value)
    detail.value.items = detail.value.items.map((row) =>
      row.id === item.id ? item : row,
    );
  void refresh().catch((error) => {
    notice.value = errorText(error);
  });
}
const searchAvailable = ref(false),
  webSearch = ref(false);
let toolsTimer: ReturnType<typeof setTimeout> | null = null;
async function syncTools() {
  try {
    const result = await auth.request<{ web_search?: { enabled: boolean } }>(
      "/api/agent/tools",
    );
    if (!disposed) {
      searchAvailable.value = !!result.web_search?.enabled;
      if (!searchAvailable.value) webSearch.value = false;
    }
  } catch {
    if (!disposed) {
      searchAvailable.value = false;
      webSearch.value = false;
    }
  } finally {
    if (!disposed) toolsTimer = setTimeout(syncTools, 15000);
  }
}
const itemForm = reactive({
  kind: "specification" as ItemKind,
  title: "",
  content: "",
  questions: "",
});
const opened = ref<ProjectItem | null>(null),
  editing = ref(false),
  actionNote = ref("");
const pending = ref<{ path: string; body: string } | null>(null);
const actionConfirmation = ref<{
  action: string;
  label: string;
  title: string;
  projectId: string;
  projectVersion: number;
  itemId: string;
  version: number;
  note: string;
} | null>(null);
const cancelActionButton = ref<HTMLButtonElement | null>(null);
let actionTrigger: HTMLElement | null = null;
watch(
  () => [
    selected.value,
    detail.value?.project.version,
    opened.value?.id,
    opened.value?.version,
    editing.value,
  ],
  () => {
    actionConfirmation.value = null;
  },
);
let timer: ReturnType<typeof setTimeout> | null = null;
let streamController: AbortController | null = null;
let generation = 0,
  disposed = false;
let itemLoadGeneration = 0;
let runLoadGeneration = 0;
let consultationTrigger: HTMLElement | null = null,
  itemTrigger: HTMLElement | null = null;

const kinds = Object.keys(kindLabels) as ItemKind[];
const discovery = computed(() => detail.value?.project.phase === "discovery");
const noteLabels = [
  ["business_background", "业务情况"],
  ["current_workflow", "现有做法"],
  ["problems", "希望改善"],
  ["users", "使用人员"],
  ["goals", "预期目标"],
  ["scope", "已讨论范围"],
  ["constraints", "明确约束"],
] as const;
const knownFacts = computed(() =>
  noteLabels
    .map(([key, label]) => {
      const value = detail.value?.project.discovery?.[key];
      return {
        key,
        label,
        text: Array.isArray(value) ? value.join("；") : value || "",
      };
    })
    .filter((row) => row.text),
);

const isDocument = computed(
  () =>
    opened.value?.kind === "specification" || opened.value?.kind === "change",
);
const isCustomer = computed(
  () => detail.value?.project.owner_id === authState.user?.id,
);
const itemNextStep = computed(() => {
  const item = opened.value;
  if (!item || !isDocument.value) return "";
  if (item.status === "approved")
    return `已发布正式 V${item.published_revision}，本版内容已锁定。后续调整请新建需求变更。`;
  if (item.status === "rejected")
    return item.kind === "specification"
      ? "负责人已驳回，请编辑当前需求草案后重新提交。"
      : "负责人已驳回，请根据处理意见整理新的草稿后重新提交。";
  if (item.status === "withdrawn")
    return item.kind === "specification"
      ? "本次申请已撤回，可编辑当前需求草案后重新提交。"
      : "本次申请已撤回，可整理新的草稿后提交。";
  if (item.questions.length)
    return `还有 ${item.questions.length} 项待确认。可先提交评审讨论；补齐后编辑草稿并重新提交，双方才能确认发布。`;
  if (item.status === "draft")
    return "下一步：提交评审。提交不会自动确认或立项，客户与负责人需要分别确认本版内容。";
  const waiting = [
    !item.customer_confirmed && "客户",
    !item.staff_confirmed && "负责人",
  ]
    .filter(Boolean)
    .join("与");
  return `已提交评审，等待${waiting}确认。双方确认后自动发布正式 V${item.base_revision + 1}。修改正文会清除已有确认，并需重新提交。`;
});
const running = computed(() => run.value?.status === "running");
const conversationRuns = computed(() => {
  const entries = new Map(
    (detail.value?.runs || []).map((entry) => [entry.id, entry]),
  );
  if (run.value) entries.set(run.value.id, run.value);
  return [...entries.values()];
});
const outgoing = computed(() => {
  if (pending.value?.path !== `/api/projects/${selected.value}/messages`)
    return "";
  return (JSON.parse(pending.value.body) as { content: string }).content;
});
const visibleItems = computed(() => {
  const items = [...(detail.value?.items || []), ...olderItems.value];
  if (tab.value === "documents")
    return items.filter((i) => ["specification", "change"].includes(i.kind));
  if (tab.value === "service")
    return items.filter((i) => ["bug", "support"].includes(i.kind));
  return items.filter(
    (i) => i.kind === (tab.value === "tasks" ? "task" : "acceptance"),
  );
});
const currentRequirement = computed(() =>
  [...(detail.value?.items || []), ...olderItems.value].find(
    (item) => item.kind === "specification" && !item.published_revision,
  ),
);
function errorText(error: unknown) {
  return error instanceof Error ? error.message : "操作未完成，请稍后重试";
}
function rememberTrigger() {
  return document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null;
}
function stopPoll() {
  streamController?.abort();
  streamController = null;
  if (timer) clearTimeout(timer);
  timer = null;
}

async function loadProjects(offset = projectOffset.value) {
  const page = await auth.request<{ items: Project[]; has_more: boolean }>(
    `/api/projects?offset=${offset}&limit=20`,
  );
  if (disposed) return;
  projects.value = page.items;
  hasMoreProjects.value = page.has_more;
  projectOffset.value = offset;
}
function syncProjectSummary(project: Project) {
  projects.value = projects.value.map((row) =>
    row.id === project.id && row.version <= project.version ? project : row,
  );
}
async function refresh() {
  if (!selected.value) return;
  const current = generation,
    id = selected.value;
  const next = await auth.request<ProjectDetail>(`/api/projects/${id}`);
  if (disposed || current !== generation) return;
  detail.value = next;
  syncProjectSummary(next.project);
  olderItems.value = [];
  hasMoreItems.value = next.has_more_items;
  const active = next.runs.find((r) => r.status === "running");
  if (active) {
    run.value = active;
    schedulePoll(id, active.id, current);
  } else run.value = next.runs[0] || null;
}
async function selectProject(id: string) {
  if (saving.value || pending.value || documentBusy.value) return;
  const current = ++generation;
  stopPoll();
  stopConversation();
  loading.value = true;
  notice.value = "";
  try {
    const [next, status] = await Promise.all([
      auth.request<ProjectDetail>(`/api/projects/${id}`),
      auth.request<{
        configured: boolean;
        tools?: { web_search?: { enabled: boolean } };
      }>(`/api/projects/${id}/agent`),
    ]);
    if (disposed || current !== generation) return;
    selected.value = id;
    detail.value = next;
    conversation.value = next.project.conversation_mode
      ? {
          mode: next.project.conversation_mode,
          version: next.project.conversation_version || 1,
          support_id: next.project.support_id || null,
          support_name: "",
          last_sequence: 0,
          facts_cursor: 0,
          extraction_pending: false,
          extraction_error: "",
        }
      : null;
    syncProjectSummary(next.project);
    configured.value = status.configured;
    searchAvailable.value = !!status.tools?.web_search?.enabled;
    webSearch.value =
      searchAvailable.value && !!next.runs[0]?.options?.web_search;
    tab.value = "conversation";
    olderItems.value = [];
    itemOffset.value = 0;
    activity.value = [];
    prompt.value = "";
    run.value =
      next.runs.find((r) => r.status === "running") || next.runs[0] || null;
    if (run.value?.status === "running")
      schedulePoll(id, run.value.id, current);
    if (conversation.value) startConversationFeed(id, current);
  } catch (error) {
    if (!disposed && current === generation) {
      notice.value = errorText(error);
      // Keep the native selector consistent with the retained workspace after a failed read.
      const selector = document.getElementById(
        "project-select",
      ) as HTMLSelectElement | null;
      if (selector) selector.value = selected.value;
      if (run.value?.status === "running")
        schedulePoll(selected.value, run.value.id, current);
      if (conversation.value) startConversationFeed(selected.value, current);
    }
  } finally {
    if (current === generation) loading.value = false;
  }
}
function schedulePoll(projectId: string, runId: string, current: number) {
  if (conversation.value) return;
  stopPoll();
  if (typeof auth.stream === "function") {
    const controller = new AbortController();
    streamController = controller;
    void followRun(projectId, runId, current, controller);
    return;
  }
  scheduleFallback(projectId, runId, current);
}
async function followRun(
  projectId: string,
  runId: string,
  current: number,
  controller: AbortController,
) {
  let finished = false;
  try {
    await auth.stream(
      `/api/projects/${projectId}/runs/${runId}/stream`,
      (event, data) => {
        if (
          controller.signal.aborted ||
          disposed ||
          current !== generation ||
          event !== "run"
        )
          return;
        const next = data as AgentRun;
        if (next.id !== runId) return;
        if (run.value?.id !== runId) return;
        if ((run.value.cache_version || 0) > (next.cache_version || 0)) {
          finished = run.value.status !== "running";
          return;
        }
        run.value = next;
        finished = next.status !== "running";
      },
      controller.signal,
    );
    if (controller.signal.aborted || disposed || current !== generation) return;
    if (finished) {
      const failed = run.value?.status === "failed" ? run.value.answer : "";
      await refresh();
      if (!disposed && current === generation && failed) notice.value = failed;
    } else
      timer = setTimeout(() => schedulePoll(projectId, runId, current), 250);
  } catch (error) {
    if (controller.signal.aborted || disposed || current !== generation) return;
    if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
      run.value = null;
      notice.value = errorText(error);
      return;
    }
    scheduleFallback(projectId, runId, current);
  }
}
function scheduleFallback(projectId: string, runId: string, current: number) {
  timer = setTimeout(async () => {
    try {
      const next = await auth.request<AgentRun>(
        `/api/projects/${projectId}/runs/${runId}`,
      );
      if (disposed || current !== generation) return;
      if (run.value?.id !== runId) return;
      if ((next.cache_version || 0) >= (run.value.cache_version || 0))
        run.value = next;
      if (next.status === "running") schedulePoll(projectId, runId, current);
      else {
        await refresh();
        if (next.status === "failed") notice.value = next.answer;
      }
    } catch (error) {
      if (!disposed && current === generation) {
        notice.value = errorText(error);
        scheduleFallback(projectId, runId, current);
      }
    }
  }, 1500);
}
async function write(path: string, data: object) {
  if (saving.value || pending.value) return null;
  pending.value = { path, body: JSON.stringify(data) };
  return performWrite();
}
async function performWrite(): Promise<Record<string, unknown> | null> {
  if (!pending.value || saving.value) return null;
  saving.value = true;
  notice.value = "";
  try {
    const result = await auth.request<Record<string, unknown>>(
      pending.value.path,
      { method: "POST", body: pending.value.body },
    );
    pending.value = null;
    return result;
  } catch (error) {
    const uncertain =
      !(error instanceof ApiError) || error.status === 0 || error.status >= 500;
    if (!uncertain) pending.value = null;
    notice.value =
      errorText(error) + (uncertain ? "。结果待确认，请使用原请求重试。" : "");
    return null;
  } finally {
    saving.value = false;
  }
}
async function retry() {
  if (starting.value) return;
  const original = pending.value?.path || "";
  if (original === "/api/consultations") starting.value = true;
  try {
    const result = await performWrite();
    if (!result) return;
    if (original.endsWith("/messages")) {
      prompt.value = "";
      if (result.delivery === "chat")
        conversation.value = result.conversation as ConversationStatus;
      else {
        run.value = result as unknown as AgentRun;
        if (run.value.status === "running")
          schedulePoll(selected.value, run.value.id, generation);
      }
    }
    if (original.endsWith("/conversation/actions")) {
      conversation.value = result.conversation as ConversationStatus;
      return;
    }
    if (original === "/api/consultations") {
      closeConsultation();
      await openCreatedConsultation(result as unknown as Project);
    } else {
      if (original.endsWith("/actions") && opened.value && !editing.value)
        await applyItemAction(result);
      else {
        closeItem();
        await refresh();
      }
    }
  } finally {
    starting.value = false;
    if (
      original === "/api/consultations" &&
      !pending.value &&
      !createdToOpen.value
    ) {
      await nextTick();
      document.getElementById("project-prompt")?.focus({ preventScroll: true });
    }
  }
}
async function startConversation() {
  if (
    recordBusy.value ||
    starting.value ||
    loading.value ||
    saving.value ||
    pending.value
  )
    return;
  if (createdToOpen.value) {
    starting.value = true;
    try {
      await openCreatedConsultation();
    } finally {
      starting.value = false;
      if (!createdToOpen.value) {
        await nextTick();
        document
          .getElementById("project-prompt")
          ?.focus({ preventScroll: true });
      }
    }
    return;
  }
  const current = detail.value;
  if (
    !admin.value &&
    current?.project.phase === "discovery" &&
    current.project.title === "需求沟通" &&
    !current.project.brief.trim() &&
    !current.messages.length &&
    !current.items.length &&
    !current.runs.length &&
    !Object.values(current.project.discovery).some((value) =>
      Array.isArray(value) ? value.length : value,
    )
  ) {
    tab.value = "conversation";
    document.getElementById("project-prompt")?.focus({ preventScroll: true });
    return;
  }
  consultationTrigger = rememberTrigger();
  if (admin.value) {
    starting.value = true;
    try {
      customers.value = (
        await auth.request<User[]>("/api/admin/users?limit=100")
      ).filter((u) => u.role !== "admin" && u.is_active);
    } catch (error) {
      notice.value = errorText(error);
      return;
    } finally {
      starting.value = false;
    }
  }
  if (admin.value) consultationDialog.value?.showModal();
  else await createConsultation();
}
function closeConsultation() {
  if (saving.value || pending.value) return;
  consultationDialog.value?.close();
  consultationTrigger?.focus();
}
function closeItem() {
  if (saving.value || pending.value || documentBusy.value) return;
  itemDialog.value?.close();
  opened.value = null;
  itemTrigger?.focus();
}
async function createConsultation() {
  if (starting.value || loading.value || saving.value || pending.value) return;
  starting.value = true;
  try {
    const result = await write("/api/consultations", {
      request_id: crypto.randomUUID(),
      owner_id: admin.value ? selectedCustomer.value : undefined,
    });
    if (!result) return;
    closeConsultation();
    selectedCustomer.value = "";
    await openCreatedConsultation(result as unknown as Project);
  } finally {
    starting.value = false;
    if (!pending.value && !createdToOpen.value) {
      await nextTick();
      document.getElementById("project-prompt")?.focus({ preventScroll: true });
    }
  }
}
async function openCreatedConsultation(created?: Project) {
  if (created) createdToOpen.value = created;
  const target = createdToOpen.value;
  if (!target) return;
  try {
    await loadProjects(0);
    await selectProject(target.id);
    if (detail.value?.project.id === target.id) createdToOpen.value = null;
  } catch (error) {
    notice.value = `沟通已创建，读取暂未完成：${errorText(error)}。请再次点击“开始需求沟通”继续打开。`;
  }
}
async function regenerateRequirements() {
  if (prompt.value.trim()) {
    notice.value = "请先发送或清空当前沟通内容，再整理需求草案";
    return;
  }
  tab.value = "conversation";
  prompt.value =
    "请根据我们已经沟通并确认的信息整理需求草案，保留尚未明确的事项，不要自动立项。";
  await send("draft");
}
function openNewItem() {
  if (!detail.value || pending.value) return;
  if (
    tab.value === "documents" &&
    !detail.value.project.revision &&
    currentRequirement.value
  ) {
    void openItem(currentRequirement.value);
    return;
  }
  itemTrigger = rememberTrigger();
  opened.value = null;
  editing.value = true;
  actionNote.value = "";
  const kind: ItemKind =
    tab.value === "service"
      ? "bug"
      : tab.value === "tasks"
        ? "task"
        : tab.value === "acceptance"
          ? "acceptance"
          : detail.value.project.revision
            ? "change"
            : "specification";
  Object.assign(itemForm, { kind, title: "", content: "", questions: "" });
  itemDialog.value?.showModal();
}
async function openItem(item: ProjectItem) {
  const current = generation,
    projectId = selected.value,
    load = ++itemLoadGeneration;
  if (item.content_loaded === false) {
    try {
      item = await auth.request<ProjectItem>(
        `/api/projects/${projectId}/items/${item.id}`,
      );
    } catch (error) {
      if (!disposed && current === generation && load === itemLoadGeneration)
        notice.value = errorText(error);
      return;
    }
    if (
      disposed ||
      current !== generation ||
      projectId !== selected.value ||
      load !== itemLoadGeneration
    )
      return;
  }
  comparison.value = null;
  itemTrigger = rememberTrigger();
  opened.value = item;
  editing.value = false;
  actionNote.value = "";
  Object.assign(itemForm, {
    kind: item.kind,
    title: item.title,
    content: item.content,
    questions: item.questions.join("\n"),
  });
  itemDialog.value?.showModal();
  itemHistory.value = [];
  try {
    const history = await auth.request<typeof itemHistory.value>(
      `/api/projects/${projectId}/activity?item_id=${item.id}`,
    );
    if (
      !disposed &&
      current === generation &&
      selected.value === projectId &&
      opened.value?.id === item.id
    )
      itemHistory.value = history;
  } catch {
    /* The record stays readable if its activity cannot be loaded. */
  }
}
async function loadRun(runId: string) {
  const current = generation,
    projectId = selected.value,
    load = ++runLoadGeneration;
  try {
    const result = await auth.request<AgentRun>(
      `/api/projects/${projectId}/runs/${runId}`,
    );
    if (
      disposed ||
      current !== generation ||
      load !== runLoadGeneration ||
      selected.value !== projectId ||
      !detail.value
    )
      return;
    const latest =
      run.value?.id === runId
        ? run.value
        : detail.value.runs.find((row) => row.id === runId);
    if ((latest?.cache_version || 0) > (result.cache_version || 0)) return;
    detail.value.runs = detail.value.runs.map((row) =>
      row.id === runId ? result : row,
    );
    if (run.value?.id === runId) run.value = result;
  } catch (error) {
    if (!disposed && current === generation) notice.value = errorText(error);
  }
}
async function compareDocument() {
  const item = opened.value;
  if (!item || comparing.value) return;
  comparing.value = true;
  try {
    const result = await auth.request<NonNullable<typeof comparison.value>>(
      `/api/projects/${selected.value}/items/${item.id}/comparison?expected_version=${item.version}`,
    );
    if (!disposed && opened.value?.id === item.id) comparison.value = result;
  } catch (error) {
    if (!disposed) notice.value = errorText(error);
  } finally {
    if (!disposed) comparing.value = false;
  }
}
async function recordsChanged(items: Project[]) {
  if (
    createdToOpen.value &&
    items.some((row) => row.id === createdToOpen.value?.id && row.deleted_at)
  )
    createdToOpen.value = null;
  const removedCurrent = items.some(
    (row) => row.id === selected.value && row.deleted_at,
  );
  const current = items.find((row) => row.id === selected.value);
  if (removedCurrent) {
    generation++;
    stopPoll();
    stopConversation();
    selected.value = "";
    detail.value = null;
    run.value = null;
    prompt.value = "";
    createdToOpen.value = null;
  } else if (current && detail.value)
    detail.value = { ...detail.value, project: current };
  try {
    await loadProjects(
      removedCurrent || !detail.value ? 0 : projectOffset.value,
    );
    if (!detail.value && projects.value[0])
      await selectProject(projects.value[0].id);
  } catch (error) {
    notice.value = `记录操作已完成，列表暂未同步：${errorText(error)}`;
  }
}
function recordLabel(row: Project) {
  if (row.title !== "需求沟通") return row.title;
  const date = new Date(row.created_at);
  const stamp = Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
  return `${row.title} · ${stamp} · ${row.id.slice(-6)}`;
}
function operationLabel(action: string) {
  return (
    (
      {
        create_item: "创建记录",
        start_consultation: "开始需求沟通",
        agent_regenerate: "重新生成需求",
        review_apply: "应用检查修改",
        review_discard: "保留原稿",
        submit: "提交评审 / 受理",
        confirm: "确认本版内容",
        edit: "编辑草稿",
        reject: "驳回",
        withdraw: "撤回",
        start: "开始处理",
        deliver: "提交处理结果",
        accept: "客户验收通过",
        reopen: "退回继续处理",
        supplement: "补充说明",
      } as Record<string, string>
    )[action.split(":")[0]] || "保存记录"
  );
}
async function saveItem() {
  if (!detail.value) return;
  const content = {
    title: itemForm.title,
    content: itemForm.content,
    questions: itemForm.questions
      .split("\n")
      .map((q) => q.trim())
      .filter(Boolean),
  };
  const result = opened.value
    ? await write(
        `/api/projects/${selected.value}/items/${opened.value.id}/actions`,
        {
          request_id: crypto.randomUUID(),
          expected_version: opened.value.version,
          action: "edit",
          ...content,
        },
      )
    : await write(`/api/projects/${selected.value}/items`, {
        request_id: crypto.randomUUID(),
        kind: itemForm.kind,
        base_revision: detail.value.project.revision,
        ...content,
      });
  if (result) {
    closeItem();
    olderItems.value = [];
    await refresh();
  }
}
async function action(value: string) {
  if (!opened.value || saving.value || pending.value || documentBusy.value)
    return;
  const label = (
    {
      confirm: "确认本版内容",
      accept: "验收通过并关闭",
      reject: "驳回",
      withdraw: "撤回",
    } as Record<string, string>
  )[value];
  if (label) {
    actionTrigger = rememberTrigger();
    actionConfirmation.value = {
      action: value,
      label,
      title: opened.value.title,
      projectId: selected.value,
      projectVersion: detail.value!.project.version,
      itemId: opened.value.id,
      version: opened.value.version,
      note: actionNote.value,
    };
    await nextTick();
    cancelActionButton.value?.focus();
    return;
  }
  await performAction(value, opened.value.version, actionNote.value);
}
async function cancelAction() {
  actionConfirmation.value = null;
  await nextTick();
  actionTrigger?.focus();
}
async function confirmAction() {
  const confirmation = actionConfirmation.value;
  if (!confirmation || saving.value || pending.value) return;
  actionConfirmation.value = null;
  if (
    confirmation.projectId !== selected.value ||
    confirmation.projectVersion !== detail.value?.project.version ||
    confirmation.itemId !== opened.value?.id ||
    confirmation.version !== opened.value.version
  ) {
    notice.value = "记录已更新，请重新查看后再操作";
    return;
  }
  await performAction(
    confirmation.action,
    confirmation.version,
    confirmation.note,
  );
}
async function performAction(value: string, version: number, note: string) {
  if (!opened.value) return;
  const result = await write(
    `/api/projects/${selected.value}/items/${opened.value.id}/actions`,
    {
      request_id: crypto.randomUUID(),
      expected_version: version,
      action: value,
      note,
    },
  );
  if (result) {
    await applyItemAction(result);
  }
}
async function applyItemAction(result: Record<string, unknown>) {
  const current = generation,
    projectId = selected.value;
  const item = result.item as ProjectItem;
  if (item && opened.value?.id === item.id) {
    opened.value = item;
    actionNote.value = "";
    comparison.value = null;
    Object.assign(itemForm, {
      title: item.title,
      content: item.content,
      questions: item.questions.join("\n"),
    });
  }
  try {
    await refresh();
    if (disposed || current !== generation || selected.value !== projectId)
      return;
    if (item && opened.value?.id === item.id) {
      const history = await auth.request<typeof itemHistory.value>(
        `/api/projects/${projectId}/activity?item_id=${item.id}`,
      );
      if (
        !disposed &&
        current === generation &&
        selected.value === projectId &&
        opened.value?.id === item.id
      )
        itemHistory.value = history;
    }
  } catch (error) {
    if (!disposed && current === generation && selected.value === projectId)
      notice.value = `操作已保存，页面资料暂未同步：${errorText(error)}`;
  }
}
async function send(
  intent: "chat" | "draft" = "chat",
  suggestion?: { id: string; accept: boolean },
) {
  const id = selected.value,
    current = generation;
  const manualChat =
    intent === "chat" &&
    !!conversation.value &&
    conversation.value.mode !== "ai";
  if (
    !prompt.value.trim() ||
    (!manualChat && (running.value || !configured.value))
  )
    return;
  const result = await write(`/api/projects/${id}/messages`, {
    request_id: crypto.randomUUID(),
    content: prompt.value.trim(),
    intent,
    web_search: searchAvailable.value && webSearch.value,
    accepted_suggestions: suggestion?.accept ? [suggestion.id] : [],
    rejected_suggestions:
      suggestion && !suggestion.accept ? [suggestion.id] : [],
  });
  if (result && !disposed && current === generation && selected.value === id) {
    prompt.value = "";
    if (result.delivery === "chat") {
      conversation.value = result.conversation as ConversationStatus;
      const snapshot = await auth.request<ConversationSnapshot>(
        `/api/projects/${id}/conversation`,
      );
      applyConversation(snapshot, id, current);
      return;
    }
    run.value = result as unknown as AgentRun;
    if (run.value.status === "running")
      schedulePoll(selected.value, run.value.id, generation);
    else await refresh();
  }
}
async function answerSuggestion(id: string, accept: boolean) {
  if (prompt.value.trim()) {
    notice.value = "请先发送或清空当前输入，再确认建议";
    return;
  }
  prompt.value = `我${accept ? "采纳" : "不采纳"}公开参考建议 ${id}，请记录我的选择。`;
  await send("chat", { id, accept });
}
async function moreItems() {
  const current = generation;
  const id = selected.value;
  const nextOffset = 40 + olderItems.value.length;
  const result = await auth.request<{
    items: ProjectItem[];
    has_more: boolean;
  }>(`/api/projects/${selected.value}/items?offset=${nextOffset}&limit=40`);
  if (disposed || current !== generation || selected.value !== id) return;
  olderItems.value.push(...result.items);
  itemOffset.value = nextOffset;
  hasMoreItems.value = result.has_more;
}
async function loadActivity() {
  tab.value = "activity";
  const current = generation;
  const id = selected.value;
  try {
    const next = await auth.request<typeof activity.value>(
      `/api/projects/${selected.value}/activity`,
    );
    if (!disposed && current === generation && selected.value === id)
      activity.value = next;
  } catch (error) {
    notice.value = errorText(error);
  }
}
function download(item: ProjectItem) {
  const blob = new Blob(
    [
      `# ${item.title}\n\n${itemVersionLabel(item)} · 状态：${itemStateLabel(item)}\n\n${item.content}\n\n## 待确认\n${item.questions.map((q) => `- ${q}`).join("\n")}`,
    ],
    { type: "text/markdown;charset=utf-8" },
  );
  const url = URL.createObjectURL(blob),
    a = document.createElement("a");
  a.href = url;
  a.download = `需求资料-${item.id}.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function allowNavigation(to: { path: string }) {
  if (
    (saving.value ||
      starting.value ||
      recordBusy.value ||
      pending.value ||
      documentBusy.value) &&
    to.path !== "/login"
  ) {
    notice.value = "请先确认当前保存结果，再离开项目";
    return false;
  }
  return true;
}
onBeforeRouteLeave(allowNavigation);
onBeforeRouteUpdate(allowNavigation);
async function openRouteProject() {
  const id = props.projectId;
  const itemId = props.initialItemId;
  const initialTab = props.initialTab;
  if (!id) return;
  closeItem();
  await selectProject(id);
  if (disposed || selected.value !== id || props.projectId !== id) return;
  const current = generation;
  if (
    [
      "conversation",
      "documents",
      "tasks",
      "acceptance",
      "service",
      "activity",
    ].includes(initialTab || "")
  )
    tab.value = initialTab!;
  if (itemId) {
    const target = await auth.request<ProjectItem>(
      `/api/projects/${id}/items/${itemId}`,
    );
    if (
      !disposed &&
      current === generation &&
      props.projectId === id &&
      props.initialItemId === itemId
    )
      await openItem(target);
  }
}
watch(
  () => [props.projectId, props.initialTab, props.initialItemId],
  () => {
    if (props.projectId)
      void openRouteProject().catch((error) => {
        if (!disposed) notice.value = errorText(error);
      });
  },
);
onMounted(async () => {
  void syncTools();
  loading.value = true;
  try {
    await loadProjects(0);
    if (props.projectId) await openRouteProject();
    else if (projects.value[0]) await selectProject(projects.value[0].id);
  } catch (error) {
    notice.value = errorText(error);
  } finally {
    if (!disposed) loading.value = false;
  }
});
onUnmounted(() => {
  disposed = true;
  generation++;
  stopPoll();
  stopConversation();
  if (toolsTimer) clearTimeout(toolsTimer);
});
</script>

<template>
  <section
    class="project-studio"
    :class="{ 'chat-workspace': detail && tab === 'conversation' }"
    :aria-busy="loading || starting"
  >
    <div class="workspace-toolbar">
      <div class="page-heading">
        <div>
          <h1>
            {{ admin ? "客户需求与项目" : "需求沟通" }}
          </h1>
          <p>先了解业务，再整理需求；双方确认后进入项目交付。</p>
        </div>
        <div class="workspace-header-actions">
          <button
            v-if="discovery"
            type="button"
            class="button button-outline notes-toggle"
            :aria-expanded="showNotes"
            @click="showNotes = !showNotes"
          >
            业务纪要
          </button>
          <a
            v-if="admin && projectId"
            class="button button-outline"
            href="#/admin/projects"
            >返回项目列表</a
          >
          <button
            v-if="!admin || !projectId"
            type="button"
            class="button button-outline"
            :disabled="recordBusy || starting || loading || saving || !!pending"
            @click="recordManager?.open()"
          >
            管理记录</button
          ><button
            v-if="!admin || !projectId"
            class="button button-dark"
            :disabled="recordBusy || starting || loading || saving || !!pending"
            @click="startConversation"
          >
            开始需求沟通
          </button>
        </div>
      </div>
      <p v-if="notice" class="notice notice-warning" role="alert">
        {{ notice }}
        <button
          v-if="pending"
          class="text-button"
          :disabled="saving"
          @click="retry"
        >
          {{ saving ? "确认中…" : "原请求重试" }}
        </button>
      </p>
      <div v-if="!admin || !projectId" class="project-selector">
        <label for="project-select">沟通记录 / 项目</label
        ><select
          id="project-select"
          :value="selected"
          :disabled="starting || loading || saving || !!pending"
          @change="selectProject(($event.target as HTMLSelectElement).value)"
        >
          <option value="" disabled>选择沟通记录或项目</option>
          <option
            v-if="detail && !projects.some((row) => row.id === selected)"
            :value="selected"
          >
            {{ recordLabel(detail.project) }}
          </option>
          <option v-for="p in projects" :key="p.id" :value="p.id">
            {{ recordLabel(p) }}{{ p.phase === "discovery" ? " · 沟通中" : "" }}
          </option></select
        ><button
          class="text-button"
          :disabled="
            projectOffset === 0 || starting || loading || saving || !!pending
          "
          @click="loadProjects(projectOffset - 20)"
        >
          上一页</button
        ><button
          class="text-button"
          :disabled="
            !hasMoreProjects || starting || loading || saving || !!pending
          "
          @click="loadProjects(projectOffset + 20)"
        >
          下一页
        </button>
        <span class="project-load-status" role="status" aria-live="polite">{{
          starting ? "正在准备沟通…" : loading ? "正在读取记录…" : ""
        }}</span>
      </div>
    </div>
    <div v-if="!detail && !loading && !starting" class="project-empty">
      <h2>暂无沟通记录</h2>
      <p>
        不需要先准备项目名称或功能清单。先说说您的业务情况，我们会逐步梳理。
      </p>
      <button
        class="button button-outline"
        :disabled="starting || loading || saving || !!pending"
        @click="startConversation"
      >
        开始需求沟通
      </button>
    </div>
    <div v-if="detail" class="project-content" :inert="loading || starting">
      <div class="project-summary">
        <div>
          <span class="project-kicker"
            >{{
              discovery
                ? "需求访谈"
                : !detail.project.stack || detail.project.stack === "待确认"
                  ? "正式项目"
                  : detail.project.stack
            }}
            ·
            {{
              detail.project.revision
                ? `已确认基线 V${detail.project.revision}`
                : "尚未立项"
            }}</span
          >
          <h2>{{ detail.project.title }}</h2>
          <p v-if="detail.project.brief">{{ detail.project.brief }}</p>
        </div>
        <button
          v-if="detail.baseline"
          class="button button-outline"
          @click="openItem(detail.baseline)"
        >
          查看当前需求书
        </button>
      </div>
      <nav class="project-tabs" aria-label="项目工作区">
        <button
          :class="{ active: tab === 'conversation' }"
          @click="tab = 'conversation'"
        >
          需求沟通</button
        ><button
          :class="{ active: tab === 'documents' }"
          @click="tab = 'documents'"
        >
          {{ discovery ? "需求草案" : "需求与变更" }}</button
        ><button
          v-if="!discovery"
          :class="{ active: tab === 'service' }"
          @click="tab = 'service'"
        >
          售后工单</button
        ><button
          v-if="!discovery"
          :class="{ active: tab === 'tasks' }"
          @click="tab = 'tasks'"
        >
          开发任务</button
        ><button
          v-if="!discovery"
          :class="{ active: tab === 'acceptance' }"
          @click="tab = 'acceptance'"
        >
          交付验收</button
        ><button :class="{ active: tab === 'activity' }" @click="loadActivity">
          操作记录
        </button>
      </nav>
      <section
        v-if="tab === 'conversation' && conversation"
        class="conversation-controls"
      >
        <div>
          <strong>{{
            conversation.mode === "ai"
              ? "AI接待中"
              : conversation.mode === "pending"
                ? "已联系人工，等待本轮AI完成"
                : conversation.support_name
                  ? `客服 ${conversation.support_name} 接待中`
                  : "已联系人工，等待客服接入"
          }}</strong>
          <p v-if="conversation.mode === 'pending'">
            本轮回复或操作会完整保留；结束后接入人工，不再开启新的自动回复。
          </p>
          <p v-if="conversation.human_only && conversation.mode !== 'ai'">
            您已选择只等人工，不会自动恢复AI。
          </p>
          <p
            v-else-if="
              conversation.mode === 'human' && conversation.deadline_at
            "
          >
            若仍无人回复或接待无互动，将于
            {{
              new Date(conversation.deadline_at).toLocaleTimeString("zh-CN", {
                hour: "2-digit",
                minute: "2-digit",
              })
            }}
            恢复AI；正在执行的操作会先完成。
          </p>
        </div>
        <button
          v-if="isCustomer && conversation.mode === 'ai'"
          class="button button-outline"
          :disabled="saving || !!pending"
          @click="controlConversation('request_human')"
        >
          联系人工
        </button>
        <template v-if="isCustomer && conversation.mode !== 'ai'">
          <button
            v-if="!conversation.human_only"
            class="text-button"
            :disabled="saving || !!pending"
            @click="controlConversation('wait_human')"
          >
            只等人工
          </button>
          <button
            v-if="conversation.mode === 'human'"
            class="text-button"
            :disabled="saving || !!pending"
            @click="controlConversation('resume_ai')"
          >
            恢复AI接待
          </button>
        </template>
        <button
          v-if="
            admin && (!conversation.support_id || conversation.mode === 'ai')
          "
          class="button button-dark"
          :disabled="saving || !!pending"
          @click="controlConversation('takeover')"
        >
          接管会话
        </button>
        <template
          v-if="
            admin &&
            conversation.support_id === authState.user?.id &&
            conversation.mode === 'human'
          "
        >
          <button
            class="button button-outline"
            :disabled="saving || !!pending"
            @click="controlConversation('release')"
          >
            交还AI
          </button>
          <button class="text-button" @click="chooseSupport">转交客服</button>
          <button
            class="text-button"
            :disabled="saving || !!pending"
            @click="controlConversation('extend')"
          >
            仍在处理，延长等待
          </button>
          <template v-if="supportChoices.length"
            ><select v-model="transferTarget" aria-label="接待客服">
              <option value="">请选择客服</option>
              <option
                v-for="user in supportChoices"
                :key="user.id"
                :value="user.id"
              >
                {{ user.display_name }}
              </option></select
            ><button
              class="button button-outline"
              :disabled="!transferTarget || saving"
              @click="controlConversation('transfer')"
            >
              确认转交
            </button></template
          >
        </template>
        <ConversationHistory
          :project-id="selected"
          :customer-id="detail.project.owner_id"
          :administrator="admin"
        />
        <p
          v-if="admin && conversation.extraction_pending"
          class="project-muted"
        >
          {{
            conversation.extraction_error ||
            "客户信息正在后台整理，聊天可正常进行。"
          }}<button
            v-if="conversation.extraction_error"
            class="text-button"
            @click="controlConversation('retry_extraction')"
          >
            重新整理信息
          </button>
        </p>
      </section>
      <div
        v-if="tab === 'conversation'"
        class="project-conversation"
        :class="{ 'conversation-wide': !discovery }"
      >
        <ConversationPanel
          v-model="prompt"
          :project-id="selected"
          :messages="detail.messages"
          :runs="conversationRuns"
          :configured="configured"
          :mode="conversation?.mode"
          :viewer-id="authState.user?.id"
          :administrator="admin"
          :support-id="conversation?.support_id"
          :locked="saving || !!pending"
          :sending="saving"
          :outgoing="outgoing"
          :search-available="searchAvailable"
          v-model:web-search="webSearch"
          @send="send"
          @load-run="loadRun"
        />
        <aside
          v-if="discovery"
          class="project-sidepanels"
          :class="{ 'notes-open': showNotes }"
        >
          <section v-if="discovery" class="discovery-notes">
            <h3>已了解的信息</h3>
            <template v-if="detail.project.consultation?.status"
              ><p class="consultation-status">
                {{
                  detail.project.consultation.memory_pending
                    ? "原话已保留，部分业务信息待整理"
                    : detail.project.consultation.draft_paused
                      ? "已暂停自动整理，您可以继续沟通"
                      : detail.project.consultation.status === "draft_ready"
                        ? "草案已整理，等待您查看"
                        : detail.project.consultation.status === "ready"
                          ? "信息已保存，需要时可整理草案"
                          : detail.project.consultation.status ===
                              "waiting_search"
                            ? "准备查询公开资料"
                            : "等待您补充业务信息"
                }}
              </p>
              <ul class="consultation-gaps">
                <li
                  v-for="gap in [
                    ...detail.project.consultation.conflicts,
                    ...detail.project.consultation.gaps,
                  ]"
                  :key="gap.key"
                >
                  <strong v-if="gap.critical">请补充：</strong
                  ><span v-else>草案中待确认：</span>{{ gap.question }}
                </li>
              </ul></template
            >
            <p v-if="!knownFacts.length" class="project-muted">
              沟通中的业务信息会逐步整理在这里。
            </p>
            <dl v-else>
              <template v-for="fact in knownFacts" :key="fact.key"
                ><dt>{{ fact.label }}</dt>
                <dd>{{ fact.text }}</dd></template
              >
            </dl>
            <template v-if="detail.project.discovery?.open_questions?.length"
              ><h4>还需确认</h4>
              <ul>
                <li
                  v-for="question in detail.project.discovery.open_questions"
                  :key="question"
                >
                  {{ question }}
                </li>
              </ul></template
            >
            <p class="project-muted">
              沟通内容会先保存；需要时直接告诉顾问“生成草案”。
            </p>
            <button
              v-if="detail.project.consultation?.status === 'draft_ready'"
              class="button button-outline"
              :disabled="
                !configured ||
                running ||
                saving ||
                !!pending ||
                !detail.messages.length
              "
              @click="regenerateRequirements"
            >
              重新整理草案
            </button>
            <section
              v-if="detail.project.consultation?.suggestions?.length"
              class="consultation-suggestions"
            >
              <h4>公开参考建议</h4>
              <article
                v-for="suggestion in detail.project.consultation.suggestions"
                :key="suggestion.id"
              >
                <p>{{ suggestion.text }}</p>
                <template
                  v-for="reference in detail.project.consultation.references.filter(
                    (row) => row.id === suggestion.reference_id,
                  )"
                  :key="reference.id"
                  ><a
                    :href="reference.url"
                    target="_blank"
                    rel="noopener noreferrer"
                    >{{ reference.title }}</a
                  ><small
                    >搜索摘要 ·
                    {{ new Date(reference.fetched_at).toLocaleString() }}</small
                  ></template
                >
                <div
                  v-if="isCustomer && suggestion.status === 'pending'"
                  class="record-row-actions"
                >
                  <button
                    class="text-button"
                    :disabled="saving || running || !!pending"
                    @click="answerSuggestion(suggestion.id, true)"
                  >
                    采纳</button
                  ><button
                    class="text-button"
                    :disabled="saving || running || !!pending"
                    @click="answerSuggestion(suggestion.id, false)"
                  >
                    不采纳
                  </button>
                </div>
                <small v-else>{{
                  suggestion.status === "accepted"
                    ? "客户已采纳，仍需评审"
                    : suggestion.status === "rejected"
                      ? "客户未采纳"
                      : "待客户确认"
                }}</small>
              </article>
            </section>
          </section>
        </aside>
      </div>
      <div v-else-if="tab === 'activity'" class="project-activity">
        <p class="project-muted">最近 60 条操作，记录操作者与处理说明。</p>
        <article v-for="event in activity" :key="event.id">
          <strong>{{ operationLabel(event.action) }}</strong>
          <p>{{ event.note || "已保存业务操作" }}</p>
          <small
            >{{ new Date(event.created_at).toLocaleString() }} ·
            {{ event.actor_id }}</small
          >
        </article>
      </div>
      <div v-else>
        <div class="project-section-heading">
          <p>
            {{
              tab === "documents"
                ? "草稿 → 提交评审 → 客户与负责人确认 → 发布新版本"
                : "草稿 → 提交 → 负责人处理 → 客户验收 → 关闭；未通过可继续处理"
            }}
          </p>
          <button
            v-if="
              admin || (!discovery && !['tasks', 'acceptance'].includes(tab))
            "
            class="button button-outline"
            :disabled="
              saving ||
              !!pending ||
              (!detail.project.revision && tab !== 'documents')
            "
            @click="openNewItem"
          >
            {{
              tab === "documents"
                ? !detail.project.revision && currentRequirement
                  ? "查看最新需求"
                  : "新建需求 / 变更"
                : tab === "service"
                  ? "提交故障 / 咨询"
                  : tab === "tasks"
                    ? "新建任务"
                    : "新建交付记录"
            }}
          </button>
        </div>
        <div class="project-item-grid">
          <button
            v-for="item in visibleItems"
            :key="item.id"
            class="project-item-card"
            @click="openItem(item)"
          >
            <div>
              <span>{{ kindLabels[item.kind] }}</span
              ><span class="project-state" :class="item.status">{{
                itemStateLabel(item)
              }}</span>
            </div>
            <h3>{{ item.title }}</h3>
            <p>{{ item.content.slice(0, 140) }}</p>
            <footer>
              <span
                >{{ itemVersionLabel(item) }} ·
                {{ item.questions.length }} 个待确认</span
              ><span>查看与处理</span>
            </footer>
            <small
              >记录 {{ item.id.slice(-8) }} ·
              {{ new Date(item.created_at).toLocaleString("zh-CN") }}</small
            >
          </button>
        </div>
        <p v-if="!visibleItems.length" class="project-muted">
          {{
            discovery
              ? "继续与业务顾问沟通，明确业务范围后可整理需求草案。"
              : "暂无记录，可根据项目进展创建或由顾问整理草稿。"
          }}
        </p>
        <button v-if="hasMoreItems" class="text-button" @click="moreItems">
          加载更早资料
        </button>
      </div>
    </div>
    <dialog
      ref="consultationDialog"
      class="project-dialog"
      @cancel.prevent="closeConsultation"
    >
      <form @submit.prevent="createConsultation">
        <div class="project-dialog-heading">
          <div>
            <h2>开始需求沟通</h2>
          </div>
          <button
            type="button"
            class="text-button"
            :disabled="saving || !!pending || documentBusy"
            @click="closeConsultation"
          >
            关闭
          </button>
        </div>
        <p class="form-description">
          选择客户后开始沟通，无需预先填写项目资料。
        </p>
        <label
          >所属客户<select v-model="selectedCustomer" required>
            <option value="" disabled>选择客户账号</option>
            <option v-for="user in customers" :key="user.id" :value="user.id">
              {{ user.display_name }} · {{ user.username }}
            </option>
          </select></label
        >
        <p v-if="pending" role="status">保存结果待确认，请关闭窗口前先重试。</p>
        <button
          class="button button-dark"
          :disabled="saving || !!pending || documentBusy"
        >
          {{ saving ? "保存中…" : "开始沟通" }}</button
        ><button
          v-if="pending"
          type="button"
          class="text-button"
          :disabled="saving"
          @click="retry"
        >
          原请求重试
        </button>
      </form>
    </dialog>
    <dialog
      ref="itemDialog"
      class="project-dialog project-document-dialog"
      @cancel.prevent="closeItem"
    >
      <div class="project-dialog-heading">
        <div>
          <p class="record-caption">项目记录</p>
          <h2>{{ opened?.title || "新建记录" }}</h2>
          <p v-if="opened">
            {{ kindLabels[opened.kind] }} · {{ itemStateLabel(opened) }} ·
            {{ itemVersionLabel(opened) }}
          </p>
        </div>
        <button
          class="text-button"
          :disabled="saving || !!pending || documentBusy"
          @click="closeItem"
        >
          关闭
        </button>
      </div>
      <p v-if="notice" class="notice notice-warning" role="alert">
        {{ notice }}
      </p>
      <button
        v-if="pending"
        class="text-button"
        :disabled="saving"
        @click="retry"
      >
        原请求重试
      </button>
      <DocumentReview
        v-if="opened && isDocument"
        v-show="!editing && !actionConfirmation"
        :key="opened.id"
        :project-id="selected"
        :item="opened"
        :configured="configured"
        :locked="saving || !!pending || editing || !!actionConfirmation"
        @changed="documentChanged"
        @busy="documentBusy = $event"
      />
      <form v-if="editing" @submit.prevent="saveItem">
        <label v-if="!opened"
          >记录类型<select v-model="itemForm.kind">
            <option
              v-for="kind in kinds.filter(
                (k) => admin || !['task', 'acceptance'].includes(k),
              )"
              :key="kind"
              :value="kind"
            >
              {{ kindLabels[kind] }}
            </option>
          </select></label
        ><label
          >标题<input
            v-model="itemForm.title"
            required
            maxlength="160" /></label
        ><label
          >{{
            itemForm.kind === "change"
              ? "变更说明与变更后的完整需求书（包含保留内容）"
              : "正文 / 业务规则 / 验收标准"
          }}<textarea
            v-model="itemForm.content"
            required
            rows="16"
            maxlength="120000"
          ></textarea></label
        ><label
          >待确认问题（每行一个，双方确认前需要补齐清空）<textarea
            v-model="itemForm.questions"
            rows="3"
          ></textarea></label
        ><button
          class="button button-dark"
          :disabled="saving || !!pending || documentBusy"
        >
          保存草稿</button
        ><button
          v-if="opened"
          type="button"
          class="text-button"
          @click="editing = false"
        >
          取消编辑
        </button>
      </form>
      <section
        v-else-if="opened && actionConfirmation"
        class="project-confirmation"
        role="group"
        aria-label="操作确认"
      >
        <h3>操作确认</h3>
        <p>
          将对「{{ actionConfirmation.title }}」执行「{{
            actionConfirmation.label
          }}」。
        </p>
        <p v-if="actionConfirmation.note">
          处理说明：{{ actionConfirmation.note }}
        </p>
        <p v-if="actionConfirmation.action === 'confirm'">
          确认表示您已审阅本版内容；双方确认后将发布正式版本。
        </p>
        <div class="project-actions">
          <button
            ref="cancelActionButton"
            class="button button-outline"
            @click="cancelAction"
          >
            取消操作
          </button>
          <button
            class="button button-dark"
            :disabled="saving || !!pending || documentBusy"
            @click="confirmAction"
          >
            确定执行
          </button>
        </div>
      </section>
      <template v-else-if="opened"
        ><div class="project-actions">
          <button class="button button-outline" @click="download(opened)">
            导出 Markdown</button
          ><button
            v-if="
              (['draft', 'submitted'].includes(opened.status) ||
                (opened.kind === 'specification' &&
                  !opened.published_revision &&
                  ['rejected', 'withdrawn'].includes(opened.status))) &&
              (isDocument || admin || opened.created_by === authState.user?.id)
            "
            class="button button-outline"
            :disabled="saving || !!pending || documentBusy"
            @click="editing = true"
          >
            编辑草稿</button
          ><button
            v-if="opened.status === 'draft'"
            class="button button-dark"
            :disabled="saving || !!pending || documentBusy"
            @click="action('submit')"
          >
            {{ isDocument ? "提交评审" : "提交受理" }}</button
          ><button
            v-if="
              isDocument &&
              opened.status === 'submitted' &&
              ((admin && !opened.staff_confirmed) ||
                (isCustomer && !opened.customer_confirmed))
            "
            class="button button-dark"
            :disabled="saving || !!pending || !!opened.questions.length"
            @click="action('confirm')"
          >
            {{ admin ? "负责人确认本版内容" : "客户确认本版内容" }}</button
          ><button
            v-if="!isDocument && admin && opened.status === 'submitted'"
            class="button button-dark"
            :disabled="saving || !!pending || documentBusy"
            @click="action('start')"
          >
            开始处理</button
          ><button
            v-if="!isDocument && admin && opened.status === 'in_progress'"
            class="button button-dark"
            :disabled="saving || !!pending || !actionNote.trim()"
            @click="action('deliver')"
          >
            提交处理结果与验收</button
          ><button
            v-if="
              !isDocument &&
              isCustomer &&
              opened.status === 'awaiting_acceptance'
            "
            class="button button-dark"
            :disabled="saving || !!pending || !actionNote.trim()"
            @click="action('accept')"
          >
            验收通过并关闭</button
          ><button
            v-if="
              !isDocument &&
              isCustomer &&
              opened.status === 'awaiting_acceptance'
            "
            class="button button-outline"
            :disabled="saving || !!pending || !actionNote.trim()"
            @click="action('reopen')"
          >
            验收未通过，继续处理
          </button>
        </div>
        <p v-if="isDocument" class="project-confirmation">
          客户：{{ opened.customer_confirmed ? "已确认" : "待确认" }} ·
          负责人：{{ opened.staff_confirmed ? "已确认" : "待确认" }}<br />{{
            itemNextStep
          }}
        </p>
        <ul v-if="opened.questions.length" class="project-questions">
          <li v-for="q in opened.questions" :key="q">待确认：{{ q }}</li>
        </ul>
        <div
          v-if="
            !['approved', 'closed', 'withdrawn', 'rejected'].includes(
              opened.status,
            )
          "
          class="project-resolution"
        >
          <label
            >处理说明 / 交付证据 / 验收意见<textarea
              v-model="actionNote"
              rows="3"
              maxlength="3000"
              placeholder="提交处理结果、验收或驳回时需要填写依据"
            ></textarea>
          </label>
          <div class="project-actions">
            <button
              v-if="admin && ['draft', 'submitted'].includes(opened.status)"
              class="text-button"
              :disabled="saving || !!pending || !actionNote.trim()"
              @click="action('reject')"
            >
              驳回并说明原因</button
            ><button
              v-if="
                opened.created_by === authState.user?.id &&
                ['draft', 'submitted'].includes(opened.status)
              "
              class="text-button"
              :disabled="saving || !!pending || !actionNote.trim()"
              @click="action('withdraw')"
            >
              撤回
            </button>
          </div>
          <button
            v-if="
              !isDocument &&
              !['closed', 'rejected', 'withdrawn'].includes(opened.status)
            "
            class="button button-outline"
            :disabled="saving || !!pending || !actionNote.trim()"
            @click="action('supplement')"
          >
            保存补充说明
          </button>
        </div>
        <div
          v-if="opened.resolution || opened.acceptance_note"
          class="project-confirmation"
        >
          <p v-if="opened.resolution">
            <strong>处理 / 交付说明：</strong>{{ opened.resolution }}
          </p>
          <p v-if="opened.acceptance_note">
            <strong>客户验收意见：</strong>{{ opened.acceptance_note }}
          </p>
        </div>
        <button
          v-if="isDocument"
          class="button button-outline"
          :disabled="comparing || saving || !!pending"
          @click="compareDocument"
        >
          {{ comparing ? "正在读取…" : "与原基线并列对比" }}
        </button>
        <div v-if="comparison" class="requirement-comparison">
          <section>
            <h3>
              {{
                comparison.base_revision
                  ? `原基线 V${comparison.base_revision}`
                  : "尚无正式基线"
              }}
            </h3>
            <pre>{{ comparison.before || "尚无已确认基线" }}</pre>
          </section>
          <section>
            <h3>
              {{
                comparison.published_revision
                  ? `本版 V${comparison.published_revision}`
                  : "拟定内容"
              }}
            </h3>
            <pre>{{ comparison.after }}</pre>
          </section>
        </div>
        <RequirementDocument :content="opened.content" />
        <details v-if="itemHistory.length" class="item-history">
          <summary>
            本记录最近 60 条处理历史（{{ itemHistory.length }}）
          </summary>
          <article v-for="event in itemHistory" :key="event.id">
            <small>{{
              new Date(event.created_at).toLocaleString("zh-CN")
            }}</small>
            <p>
              {{ operationLabel(event.action)
              }}{{ event.note ? `：${event.note}` : "" }}
            </p>
          </article>
        </details>
      </template>
    </dialog>
    <RecordManager
      ref="recordManager"
      :admin="admin"
      @busy="recordBusy = $event"
      @changed="recordsChanged"
    />
  </section>
</template>

<style scoped>
.conversation-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  margin-bottom: 12px;
  border: 1px solid #dce5ef;
  border-radius: 6px;
  background: #f7faff;
}
.conversation-controls p {
  margin: 4px 0;
  line-height: 1.6;
}
</style>
