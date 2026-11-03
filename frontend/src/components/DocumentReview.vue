<script setup lang="ts">
import { requestId } from "../api/request-id";
import { computed, onUnmounted, ref, watch } from "vue";
import { auth } from "../auth/session";
import { ApiError } from "../auth/client";
import type {
  ProjectItem,
  DocumentReview,
  DocumentRevision,
} from "../api/projects";

const props = defineProps<{
  projectId: string;
  item: ProjectItem;
  configured: boolean;
  locked: boolean;
}>();
const emit = defineEmits<{
  changed: [item: ProjectItem];
  busy: [value: boolean];
}>();
const review = ref<DocumentReview | null>(null),
  history = ref<DocumentRevision[]>([]);
const notice = ref(""),
  writing = ref(false),
  historyOpen = ref(false);
const pending = ref<{ path: string; body: string; operation: string } | null>(
  null,
);
const restoreTarget = ref<DocumentRevision | null>(null);
const base = computed(
  () => `/api/projects/${props.projectId}/items/${props.item.id}`,
);
const editable = computed(() =>
  ["draft", "submitted"].includes(props.item.status),
);
const checking = computed(() => review.value?.status === "running");
const stale = computed(
  () => !!review.value && review.value.source_version !== props.item.version,
);
const hasChanges = computed(
  () =>
    !!review.value?.report.changes?.length ||
    !!review.value?.report.questions?.length,
);
let timer: ReturnType<typeof setTimeout> | null = null;
let generation = 0,
  disposed = false;
watch(
  () => writing.value || !!pending.value,
  (value) => emit("busy", value),
  { immediate: true },
);
function errorText(error: unknown) {
  return error instanceof Error ? error.message : "操作暂未完成";
}
function clearTimer() {
  if (timer) clearTimeout(timer);
  timer = null;
}
async function load() {
  const current = ++generation,
    path = base.value;
  clearTimer();
  review.value = null;
  notice.value = "";
  restoreTarget.value = null;
  try {
    const next = await auth.request<DocumentReview | null>(`${path}/reviews`);
    if (disposed || current !== generation) return;
    if (next?.id && next.status && next.report && Array.isArray(next.events)) {
      review.value = next;
      if (next.status === "running") poll(current);
    }
  } catch (error) {
    if (!disposed && current === generation) notice.value = errorText(error);
  }
}
watch(
  () => `${props.projectId}:${props.item.id}`,
  () => {
    history.value = [];
    historyOpen.value = false;
    void load();
  },
  { immediate: true },
);
function poll(current: number) {
  clearTimer();
  timer = setTimeout(async () => {
    const id = review.value?.id,
      path = base.value;
    if (!id || disposed || current !== generation) return;
    try {
      const next = await auth.request<DocumentReview>(`${path}/reviews/${id}`);
      if (disposed || current !== generation || review.value?.id !== id) return;
      if (!next?.report || !Array.isArray(next.events))
        throw new ApiError("检查结果暂未读取完整");
      review.value = next;
      if (next.status === "running") poll(current);
    } catch (error) {
      if (disposed || current !== generation) return;
      notice.value = errorText(error);
      if (
        !(error instanceof ApiError) ||
        ![401, 403, 404].includes(error.status)
      )
        poll(current);
    }
  }, 1200);
}
async function mutate(path: string, data: object, operation: string) {
  if (writing.value || pending.value || props.locked) return;
  pending.value = { path, body: JSON.stringify(data), operation };
  await retry();
}
async function retry() {
  if (!pending.value || writing.value || props.locked) return;
  const current = generation,
    command = pending.value;
  writing.value = true;
  notice.value = "";
  try {
    const result = await auth.request<
      DocumentReview | { item: ProjectItem; review?: DocumentReview }
    >(command.path, { method: "POST", body: command.body });
    if (disposed || current !== generation) return;
    pending.value = null;
    if (command.operation === "start") {
      review.value = result as DocumentReview;
      if (review.value.status === "running") poll(current);
    } else {
      const saved = result as { item: ProjectItem; review?: DocumentReview };
      if (saved.review) review.value = saved.review;
      else {
        review.value = null;
        historyOpen.value = false;
      }
      emit("changed", saved.item);
      notice.value =
        command.operation === "restore"
          ? "历史内容已恢复为一次新修订。"
          : saved.review?.status === "discarded"
            ? "已保留当前正文。"
            : "修改已应用，原稿已保存；需要重新提交评审。";
      restoreTarget.value = null;
      if (historyOpen.value) await loadHistory();
    }
  } catch (error) {
    if (disposed || current !== generation) return;
    if (error instanceof ApiError && error.status > 0 && error.status < 500)
      pending.value = null;
    notice.value = errorText(error);
  } finally {
    if (current === generation) writing.value = false;
  }
}
function start() {
  return mutate(
    `${base.value}/reviews`,
    { request_id: requestId(), expected_version: props.item.version },
    "start",
  );
}
function decide(decision: "apply" | "discard") {
  if (!review.value) return;
  return mutate(
    `${base.value}/reviews/${review.value.id}/decision`,
    {
      request_id: requestId(),
      expected_version: props.item.version,
      decision,
    },
    "decision",
  );
}
async function loadHistory() {
  const current = generation;
  try {
    const rows = await auth.request<DocumentRevision[]>(
      `${base.value}/history`,
    );
    if (!disposed && current === generation)
      history.value = Array.isArray(rows) ? rows : [];
  } catch (error) {
    if (!disposed && current === generation) notice.value = errorText(error);
  }
}
function showHistory() {
  historyOpen.value = !historyOpen.value;
  if (historyOpen.value) void loadHistory();
}
function restore() {
  const target = restoreTarget.value;
  if (!target) return;
  return mutate(
    `${base.value}/history/${target.id}/restore`,
    { request_id: requestId(), expected_version: props.item.version },
    "restore",
  );
}
function blockTitle(id: string) {
  return (
    review.value?.report.blocks?.find((b) => b.id === id)?.title || "相关章节"
  );
}
onUnmounted(() => {
  disposed = true;
  generation++;
  clearTimer();
  emit("busy", false);
});
</script>

<template>
  <section class="document-review" aria-label="文档检查与历史">
    <div class="review-toolbar">
      <button
        class="button button-outline"
        :disabled="!configured || checking || locked || writing || !!pending"
        @click="start"
      >
        {{ checking ? "正在检查…" : "检查并优化" }}
      </button>
      <button
        class="text-button"
        :disabled="locked || writing || !!pending"
        @click="showHistory"
      >
        {{ historyOpen ? "收起历史" : "历史内容" }}
      </button>
      <span v-if="checking" role="status">{{
        review?.events.at(-1)?.summary || "正在按章节检查，原稿保持不变…"
      }}</span>
    </div>
    <p v-if="notice" class="notice notice-warning" role="status">
      {{ notice }}
    </p>
    <p v-if="pending && !writing" class="notice notice-warning">
      本次操作结果待确认，请用原请求重试。<button
        class="text-button"
        :disabled="locked"
        @click="retry"
      >
        原请求重试
      </button>
    </p>
    <template v-if="review && review.status !== 'running'">
      <p v-if="review.status === 'failed'" role="alert">
        {{ review.error || "检查暂未完成，原稿保持不变。" }}
      </p>
      <div v-else class="review-result">
        <p>
          <strong>{{
            review.status === "applied"
              ? review.candidate === item.content
                ? "优化修改已应用"
                : "历史检查记录，正文已另行修改"
              : review.status === "discarded"
                ? "已保留原稿"
                : review.report.partial && !review.report.findings?.length
                  ? "本次检查未完成，原稿保持不变"
                  : "检查结果已整理，正文尚未改变"
          }}</strong>
        </p>
        <p>{{ review.report.summary }}</p>
        <p>
          建议修正 {{ review.report.suggested_fixes || 0 }} 处，还有
          {{ review.report.questions?.length || 0 }} 项需要您决定。
        </p>
        <p v-if="review.report.unfixed_ids?.length">
          另有
          {{ review.report.unfixed_ids.length }}
          处问题尚未修正，已保留在检查依据中。
        </p>
        <p v-if="review.report.partial" class="notice notice-warning">
          本次仅完成部分检查，仍有内容待核对。
        </p>
        <ul
          v-if="review.report.stop_reasons?.length"
          class="notice notice-warning"
        >
          <li v-for="reason in review.report.stop_reasons" :key="reason">
            {{ reason }}
          </li>
        </ul>
        <p
          v-if="!review.report.verified && review.report.changes?.length"
          class="notice notice-warning"
        >
          候选修改尚未通过复核，请保留原稿或重新检查。
        </p>
        <p
          v-if="stale && review.status === 'ready'"
          class="notice notice-warning"
        >
          正文已变化，这份候选稿已保留，请按最新正文重新检查。
        </p>
        <details v-if="review.report.changes?.length" class="review-diff">
          <summary>
            查看修改差异（{{ review.report.changes.length }} 处）
          </summary>
          <article
            v-for="(change, index) in review.report.changes"
            :key="index"
          >
            <h4>{{ blockTitle(change.block_id) }}</h4>
            <div class="review-diff-columns">
              <section>
                <strong>原文</strong>
                <pre>{{ change.old_text }}</pre>
              </section>
              <section>
                <strong>建议修改</strong>
                <pre>{{ change.new_text || "移除此处内容" }}</pre>
              </section>
            </div>
          </article>
        </details>
        <details v-if="review.report.findings?.length">
          <summary>
            查看检查依据（{{ review.report.findings.length }} 项）
          </summary>
          <article
            v-for="finding in review.report.findings"
            :key="finding.id"
            class="review-finding"
          >
            <strong>{{
              finding.kind === "decision"
                ? "需要您决定"
                : finding.kind === "suggestion"
                  ? "可选建议"
                  : "内容修正"
            }}</strong>
            <p>{{ finding.explanation }}</p>
            <blockquote>{{ finding.evidence }}</blockquote>
            <p>{{ finding.recommendation }}</p>
            <p v-if="finding.source_quote">
              客户依据：{{ finding.source_quote }}
            </p>
          </article>
        </details>
        <ul v-if="review.report.questions?.length">
          <li v-for="question in review.report.questions" :key="question">
            {{ question }}
          </li>
        </ul>
        <div v-if="review.status === 'ready'" class="review-toolbar">
          <button
            class="button button-dark"
            :disabled="
              !editable ||
              stale ||
              !review.report.verified ||
              !hasChanges ||
              locked ||
              writing ||
              !!pending
            "
            @click="decide('apply')"
          >
            应用修改
          </button>
          <button
            class="button button-outline"
            :disabled="locked || writing || !!pending"
            @click="decide('discard')"
          >
            暂不应用
          </button>
          <small>{{
            editable
              ? "应用后保存旧稿，并清空已有确认；不会直接发布。"
              : "正式版本保持锁定，请通过新变更修改。"
          }}</small>
        </div>
      </div>
    </template>
    <section v-if="historyOpen" aria-label="历史内容">
      <p v-if="!history.length" class="project-muted">
        尚无历史修订；首次修改前会保留原稿。
      </p>
      <details
        v-for="revision in history"
        :key="revision.id"
        class="review-history"
      >
        <summary>
          {{ revision.title }} ·
          {{ new Date(revision.created_at).toLocaleString() }}
        </summary>
        <pre>{{ revision.content }}</pre>
        <button
          class="button button-outline"
          :disabled="!editable || locked || writing || !!pending"
          @click="restoreTarget = revision"
        >
          恢复此稿
        </button>
      </details>
      <div
        v-if="restoreTarget"
        class="review-restore"
        role="group"
        aria-label="恢复历史内容确认"
      >
        <p>
          恢复「{{
            restoreTarget.title
          }}」的历史内容？当前正文会保留为新历史，已有双方确认将清空。
        </p>
        <button
          class="button button-outline"
          :disabled="writing || !!pending"
          @click="restoreTarget = null"
        >
          取消恢复
        </button>
        <button
          class="button button-dark"
          :disabled="locked || writing || !!pending"
          @click="restore"
        >
          确认恢复内容
        </button>
      </div>
    </section>
  </section>
</template>

<style scoped>
.document-review {
  margin: 1rem 0;
  padding: 1rem;
  border: 1px solid #dbe3ee;
  border-radius: 6px;
  background: #f8fafc;
}
.review-toolbar {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  flex-wrap: wrap;
}
.review-result > p {
  margin: 0.65rem 0;
}
.review-diff,
.review-history,
.review-finding {
  margin: 0.8rem 0;
}
summary {
  cursor: pointer;
  color: #285b9d;
  padding: 0.4rem 0;
}
.review-diff-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}
.review-diff-columns section {
  min-width: 0;
  padding: 0.75rem;
  border: 1px solid #dbe3ee;
  background: white;
}
.review-diff-columns section:last-child {
  border-color: #b8d6c6;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: inherit;
  line-height: 1.6;
  max-height: 28rem;
  overflow: auto;
}
blockquote {
  border-left: 3px solid #bacbe0;
  margin: 0.6rem 0;
  padding-left: 0.7rem;
  color: #52657c;
}
.review-restore {
  padding: 1rem;
  border: 1px solid #b9cbe1;
}
@media (max-width: 760px) {
  .review-diff-columns {
    grid-template-columns: 1fr;
  }
}
</style>
