<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import type { AgentRun, ProjectMessage } from "../api/projects";
import AgentTrace from "./AgentTrace.vue";
import RequirementDocument from "./RequirementDocument.vue";

const props = defineProps<{
  projectId: string;
  messages: ProjectMessage[];
  runs: AgentRun[];
  modelValue: string;
  configured: boolean;
  locked: boolean;
  sending: boolean;
  outgoing: string;
  searchAvailable?: boolean;
  webSearch?: boolean;
  mode?: "ai" | "pending" | "human";
  viewerId?: string;
  administrator?: boolean;
  supportId?: string | null;
}>();
const emit = defineEmits<{
  "update:modelValue": [value: string];
  send: [];
  "load-run": [runId: string];
  "update:webSearch": [value: boolean];
}>();
const viewport = ref<HTMLElement | null>(null),
  followLatest = ref(true);
const running = computed(() =>
  props.runs.some((run) => run.status === "running"),
);
const sendDisabled = computed(
  () =>
    (props.administrator &&
      !(props.mode === "human" && props.supportId === props.viewerId)) ||
    ((!props.mode || props.mode === "ai") &&
      (!props.configured || running.value)) ||
    props.locked ||
    !props.modelValue.trim(),
);
const timeline = computed(() => {
  const byId = new Map(props.runs.map((run) => [run.id, run]));
  const entries: {
    id: string;
    role: string;
    content: string;
    at: string;
    run?: AgentRun;
    senderId?: string | null;
    senderType?: string;
    senderName?: string;
  }[] = props.messages.map((message) => ({
    id: message.run_id
      ? `${message.run_id}-${message.role === "user" ? "prompt" : "response"}`
      : message.id,
    role: message.role,
    content: message.content,
    at: message.created_at || "",
    senderId: message.sender_id,
    senderType: message.sender_type,
    senderName: message.sender_name,
    run:
      message.role === "assistant" && message.run_id
        ? byId.get(message.run_id)
        : undefined,
  }));
  for (const run of props.runs) {
    if (run.status === "completed") continue;
    const related = props.messages.filter(
      (message) => message.run_id === run.id,
    );
    if (
      !related.some(
        (message) =>
          message.role === "user" || message.sender_type === "support",
      )
    ) {
      entries.push({
        id: `${run.id}-prompt`,
        role: "user",
        content: run.prompt,
        at: run.created_at,
        senderId: run.actor_id,
        senderType: "customer",
      });
    }
    if (
      !related.some(
        (message) =>
          message.role === "assistant" &&
          (!message.sender_type ||
            ["ai", "legacy"].includes(message.sender_type)),
      )
    ) {
      entries.push({
        id: `${run.id}-response`,
        role: "assistant",
        content: run.status === "failed" ? run.answer : "",
        at: run.created_at,
        run,
      });
    }
  }
  // Legacy messages have no run relation; keep their stored order without guessing from repeated text.
  return entries.sort((a, b) => a.at.localeCompare(b.at));
});
function senderLabel(entry: {
  role: string;
  senderId?: string | null;
  senderType?: string;
  senderName?: string;
}) {
  if (entry.senderType === "system") return "系统通知";
  if (entry.senderType === "support")
    return entry.senderId === props.viewerId
      ? "您（客服）"
      : `客服 ${entry.senderName || ""}`;
  if (entry.senderType === "customer")
    return entry.senderId === props.viewerId
      ? "您"
      : `客户 ${entry.senderName || ""}`;
  if (entry.senderType === "legacy") return "历史记录";
  if (entry.senderType === "ai") return "AI顾问";
  return entry.role === "user"
    ? props.administrator
      ? "客户"
      : "您"
    : "业务顾问";
}
function onScroll() {
  const element = viewport.value;
  if (element)
    followLatest.value =
      element.scrollHeight - element.scrollTop - element.clientHeight < 64;
}
async function toLatest() {
  followLatest.value = true;
  await nextTick();
  if (viewport.value) viewport.value.scrollTop = viewport.value.scrollHeight;
}
watch(
  () => props.projectId,
  async () => {
    await nextTick();
    void toLatest();
  },
);
watch(
  () => [
    props.messages,
    props.runs
      .map(
        (run) =>
          `${run.id}:${run.status}:${run.events.length}:${run.preview?.kind || ""}:${run.preview?.title || ""}:${run.preview?.kind === "reply" ? run.preview.text.length : 0}`,
      )
      .join("|"),
    props.outgoing,
    props.sending,
  ],
  () => {
    if (followLatest.value) void toLatest();
  },
  { immediate: true, flush: "post" },
);
function keydown(event: KeyboardEvent) {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (!sendDisabled.value) emit("send");
}
</script>

<template>
  <div class="project-chat">
    <header class="chat-heading">
      <h3>
        {{
          mode === "human"
            ? "人工客服"
            : mode === "pending"
              ? "等待人工接入"
              : "业务顾问"
        }}
      </h3>
      <span>需求沟通</span>
    </header>
    <div
      ref="viewport"
      class="chat-viewport"
      role="region"
      aria-label="对话内容"
      tabindex="0"
      @scroll.passive="onScroll"
    >
      <article
        v-if="!timeline.length"
        class="project-message assistant chat-welcome"
      >
        <small>业务顾问</small>
        <p>您好，我们先聊聊您的业务。</p>
        <p>您目前主要做什么业务？现在有哪些事情比较费时，或者希望改善？</p>
      </article>
      <p
        v-if="!configured && (!mode || mode === 'ai')"
        class="notice notice-warning"
      >
        业务顾问暂时不可用，请联系负责人。您仍可在“需求与变更”中手动整理草稿。
      </p>
      <article
        v-for="entry in timeline"
        :key="entry.id"
        class="project-message"
        :class="
          entry.senderId && entry.senderId === viewerId
            ? 'user'
            : administrator && entry.role === 'user'
              ? 'assistant'
              : entry.role
        "
      >
        <small>{{ senderLabel(entry) }}</small>
        <p v-if="entry.role === 'user'">{{ entry.content }}</p>
        <template v-else
          ><button
            v-if="entry.run?.details_loaded === false"
            class="text-button chat-trace-toggle"
            @click="emit('load-run', entry.run.id)"
          >
            查看处理过程
          </button>
          <AgentTrace v-else-if="entry.run" :run="entry.run" />
          <section
            v-if="
              entry.run?.status === 'running' &&
              entry.run.preview &&
              (entry.run.preview.kind === 'document' || entry.run.preview.text)
            "
            class="chat-live-preview"
            aria-label="正在生成的内容"
          >
            <p class="project-muted" role="status">
              {{
                entry.run.preview.kind === "document"
                  ? `正在撰写：${entry.run.preview.title || "需求草案"}`
                  : "正在回复…"
              }}
              {{
                entry.run.preview.kind === "document"
                  ? " · 完成后可查看草案"
                  : " · 内容尚在整理"
              }}
            </p>
            <pre
              v-if="entry.run.preview.kind === 'reply'"
              class="chat-stream-text"
              >{{ entry.run.preview.text }}</pre>
          </section>
          <p
            v-if="entry.run?.status === 'failed'"
            class="chat-error"
            role="alert"
          >
            {{ entry.content }}
          </p>
          <RequirementDocument
            v-else-if="entry.content"
            :content="entry.content"
        /></template>
      </article>
      <template v-if="outgoing">
        <article class="project-message user chat-outgoing">
          <small>您</small>
          <p>{{ outgoing }}</p>
        </article>
        <article class="project-message assistant">
          <small>业务顾问</small>
          <p class="project-muted" role="status">
            {{ sending ? "正在发送…" : "发送结果待确认，请使用原请求重试。" }}
          </p>
        </article>
      </template>
    </div>
    <div v-if="!followLatest" class="chat-jump">
      <button class="text-button" @click="toLatest">回到最新消息 ↓</button>
    </div>
    <form
      class="project-composer"
      @submit.prevent="!sendDisabled && emit('send')"
    >
      <label for="project-prompt" class="sr-only">沟通内容</label>
      <textarea
        id="project-prompt"
        :value="modelValue"
        maxlength="5000"
        rows="2"
        placeholder="输入业务情况或回复顾问的问题…"
        :disabled="locked"
        @input="
          emit(
            'update:modelValue',
            ($event.target as HTMLTextAreaElement).value,
          )
        "
        @keydown="keydown"
      ></textarea>
      <div>
        <div class="composer-options">
          <label
            v-if="searchAvailable && (!mode || mode === 'ai')"
            class="web-search-option"
            title="仅查询公开资料，结果作为待确认建议"
            ><input
              type="checkbox"
              :checked="webSearch"
              :disabled="locked || running"
              @change="
                emit(
                  'update:webSearch',
                  ($event.target as HTMLInputElement).checked,
                )
              "
            />联网补充</label
          ><small>Enter 发送 · Shift + Enter 换行</small>
        </div>
        <button class="button button-dark" :disabled="sendDisabled">
          {{ running && (!mode || mode === "ai") ? "正在处理…" : "发送" }}
        </button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.chat-trace-toggle {
  display: block;
  margin: 0.45rem 0;
}
.chat-stream-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  line-height: 1.75;
  margin: 0.6rem 0;
}
</style>
