<script setup lang="ts">
import { onUnmounted, ref, watch } from "vue";
import { auth } from "../auth/session";
import type { ProjectMessage } from "../api/projects";

type HistoryRow = ProjectMessage & {
  project_id: string;
  project_title?: string;
};
const props = defineProps<{
  projectId: string;
  customerId: string;
  administrator?: boolean;
}>();
const dialog = ref<HTMLDialogElement | null>(null),
  rows = ref<HistoryRow[]>([]),
  context = ref<HistoryRow[]>([]);
const query = ref(""),
  sender = ref(""),
  allProjects = ref(false),
  start = ref(""),
  end = ref("");
const loading = ref(false),
  notice = ref(""),
  hasMore = ref(false),
  cursor = ref("");
let generation = 0,
  contextGeneration = 0,
  disposed = false,
  trigger: HTMLElement | null = null;

function label(row: HistoryRow) {
  const names: Record<string, string> = {
    customer: "客户",
    support: "客服",
    ai: "AI顾问",
    system: "系统通知",
    legacy: "历史记录",
  };
  return `${names[row.sender_type || "legacy"]} ${row.sender_name || ""}`.trim();
}
async function load(more = false) {
  const current = ++generation;
  contextGeneration++;
  loading.value = true;
  notice.value = "";
  context.value = [];
  const params = new URLSearchParams({
    customer_id: props.customerId,
    q: query.value,
    sender: sender.value,
    limit: "40",
  });
  if (more) params.set("cursor", cursor.value);
  if (start.value)
    params.set("start", new Date(`${start.value}T00:00:00`).toISOString());
  if (end.value) {
    const exclusiveEnd = new Date(`${end.value}T00:00:00`);
    exclusiveEnd.setDate(exclusiveEnd.getDate() + 1);
    params.set("end", exclusiveEnd.toISOString());
  }
  if (!allProjects.value) params.set("project_id", props.projectId);
  try {
    const result = await auth.request<{
      items: HistoryRow[];
      has_more: boolean;
      next_cursor: string;
    }>(`/api/conversations/history?${params}`);
    if (disposed || current !== generation) return;
    rows.value = more ? [...rows.value, ...result.items] : result.items;
    cursor.value = result.next_cursor;
    hasMore.value = result.has_more;
  } catch (error) {
    if (!disposed && current === generation)
      notice.value =
        error instanceof Error ? error.message : "聊天记录读取失败";
  } finally {
    if (!disposed && current === generation) loading.value = false;
  }
}
async function showContext(row: HistoryRow) {
  const current = generation,
    request = ++contextGeneration;
  try {
    const result = await auth.request<{ items: HistoryRow[] }>(
      `/api/projects/${row.project_id}/messages/${row.id}/context`,
    );
    if (!disposed && current === generation && request === contextGeneration)
      context.value = result.items;
  } catch (error) {
    if (!disposed && current === generation && request === contextGeneration)
      notice.value = error instanceof Error ? error.message : "上下文读取失败";
  }
}
function open() {
  trigger =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  dialog.value?.showModal();
  void load();
}
function close() {
  dialog.value?.close();
  generation++;
  contextGeneration++;
  loading.value = false;
  trigger?.focus();
}
watch(() => props.projectId, close);
onUnmounted(() => {
  disposed = true;
  generation++;
  contextGeneration++;
});
</script>

<template>
  <button class="text-button" @click="open">查询聊天记录</button>
  <dialog ref="dialog" class="history-dialog" @cancel.prevent="close">
    <header>
      <h2>聊天记录查询</h2>
      <button class="text-button" @click="close">关闭</button>
    </header>
    <form class="history-filters" @submit.prevent="load(false)">
      <label>关键词<input v-model="query" maxlength="120" /></label>
      <label
        >发言者<select v-model="sender">
          <option value="">全部</option>
          <option value="customer">客户</option>
          <option value="support">客服</option>
          <option value="ai">AI顾问</option>
          <option value="system">系统通知</option>
        </select></label
      >
      <label>开始日期<input v-model="start" type="date" /></label
      ><label>结束日期<input v-model="end" type="date" /></label>
      <label
        ><input v-model="allProjects" type="checkbox" />该客户全部项目</label
      >
      <button class="button button-outline" :disabled="loading">查询</button>
    </form>
    <p v-if="notice" class="notice notice-warning" role="alert">{{ notice }}</p>
    <p v-if="loading" role="status">正在读取…</p>
    <section v-if="context.length" class="history-context">
      <h3>原话与前后沟通</h3>
      <article v-for="row in context" :key="row.id">
        <small
          >{{ label(row) }} ·
          {{ new Date(row.created_at || "").toLocaleString("zh-CN") }}</small
        >
        <pre>{{ row.content }}</pre>
      </article>
    </section>
    <article v-for="row in rows" :key="row.id" class="history-row">
      <small
        >{{ label(row) }} ·
        {{ new Date(row.created_at || "").toLocaleString("zh-CN")
        }}<span v-if="row.project_title">
          · {{ row.project_title }}</span
        ></small
      >
      <pre>{{ row.content }}</pre>
      <button class="text-button" @click="showContext(row)">
        定位原话与上下文
      </button>
    </article>
    <p v-if="!loading && !rows.length">暂无符合条件的聊天记录。</p>
    <button
      v-if="hasMore"
      class="button button-outline"
      :disabled="loading"
      @click="load(true)"
    >
      加载更早记录
    </button>
  </dialog>
</template>

<style scoped>
.history-dialog {
  width: min(960px, 94vw);
  max-height: 88vh;
  border: 1px solid #dbe3ee;
  border-radius: 8px;
  padding: 24px;
  overflow: auto;
}
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.history-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: end;
}
.history-filters label {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.history-row,
.history-context {
  padding: 14px;
  margin-top: 12px;
  border: 1px solid #e2e8f0;
  border-radius: 5px;
}
.history-context {
  background: #f1f6fe;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  line-height: 1.7;
}
</style>
