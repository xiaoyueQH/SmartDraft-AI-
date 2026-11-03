<script setup lang="ts">
import { requestId } from "../api/request-id";
import { computed, onUnmounted, ref, watch } from "vue";
import { auth } from "../auth/session";
import { ApiError } from "../auth/client";
import type { ProjectItem } from "../api/projects";

const props = defineProps<{
  projectId: string;
  item: ProjectItem;
  locked: boolean;
}>();
const emit = defineEmits<{
  changed: [item: ProjectItem];
  busy: [value: boolean];
}>();
interface Change {
  change: null | {
    id: string;
    edits: {
      block_id: string;
      title: string;
      old_text: string;
      new_text: string;
    }[];
  };
  can_undo: boolean;
}
const latest = ref<Change | null>(null),
  notice = ref(""),
  writing = ref(false);
const pending = ref<{ path: string; body: string } | null>(null);
const count = computed(
  () => new Set(latest.value?.change?.edits.map((e) => e.block_id)).size,
);
let generation = 0,
  disposed = false;
watch(
  () => writing.value || !!pending.value,
  (value) => emit("busy", value),
  { immediate: true },
);
watch(
  () => [props.projectId, props.item.id],
  (value, previous) => {
    if (value[0] !== previous[0] || value[1] !== previous[1]) {
      pending.value = null;
      notice.value = "";
    }
  },
);
watch(
  () => [props.projectId, props.item.id, props.item.version],
  async () => {
    const current = ++generation;
    latest.value = null;
    if (!props.projectId) return;
    try {
      const result = await auth.request<Change>(
        `/api/projects/${props.projectId}/items/${props.item.id}/latest-change`,
      );
      if (!disposed && current === generation) latest.value = result;
    } catch (error) {
      if (!disposed && current === generation)
        notice.value =
          error instanceof Error ? error.message : "修改记录暂未读取";
    }
  },
  { immediate: true },
);
async function undo() {
  if (
    props.locked ||
    writing.value ||
    pending.value ||
    !latest.value?.change ||
    !latest.value.can_undo
  )
    return;
  pending.value = {
    path: `/api/projects/${props.projectId}/items/${props.item.id}/undo-change`,
    body: JSON.stringify({
      request_id: requestId(),
      expected_version: props.item.version,
      change_id: latest.value.change.id,
    }),
  };
  await retry();
}
async function retry() {
  if (!pending.value || writing.value) return;
  const command = pending.value,
    projectId = props.projectId,
    itemId = props.item.id;
  writing.value = true;
  notice.value = "";
  try {
    const result = await auth.request<{ item: ProjectItem }>(command.path, {
      method: "POST",
      body: command.body,
    });
    if (disposed || projectId !== props.projectId || itemId !== props.item.id)
      return;
    pending.value = null;
    latest.value = null;
    if (result.item.version >= props.item.version) emit("changed", result.item);
    notice.value =
      result.item.version >= props.item.version
        ? "已撤销最近一次修改，客户原话仍保留；最新要求可再次更新到需求书。"
        : "原撤销请求已处理，页面保留后来更新的内容。";
  } catch (error) {
    if (disposed || projectId !== props.projectId || itemId !== props.item.id)
      return;
    notice.value = error instanceof Error ? error.message : "本次撤销暂未完成";
    if (
      error instanceof ApiError &&
      [400, 401, 403, 404, 409, 422].includes(error.status)
    )
      pending.value = null;
  } finally {
    writing.value = false;
  }
}
onUnmounted(() => {
  disposed = true;
  generation++;
  emit("busy", false);
});
</script>

<template>
  <section v-if="latest?.change || notice || pending" class="document-change">
    <p v-if="latest?.change">
      最近一次更新了 {{ count }} 处内容，其他正文保留。
    </p>
    <details v-if="latest?.change">
      <summary>查看最近修改差异</summary>
      <article v-for="(edit, i) in latest.change.edits" :key="i">
        <strong>{{ edit.title }}</strong>
        <div class="change-columns">
          <div>
            <span>修改前</span>
            <pre>{{ edit.old_text || "（新增）" }}</pre>
          </div>
          <div>
            <span>修改后</span>
            <pre>{{ edit.new_text || "（删除）" }}</pre>
          </div>
        </div>
      </article>
    </details>
    <button
      v-if="latest?.can_undo"
      class="button button-outline"
      :disabled="locked || writing || !!pending"
      @click="undo"
    >
      撤销本次修改
    </button>
    <p v-if="notice" role="status">{{ notice }}</p>
    <button
      v-if="pending"
      class="text-button"
      :disabled="writing"
      @click="retry"
    >
      原请求重试
    </button>
  </section>
</template>

<style scoped>
.document-change {
  padding: 1rem;
  margin: 1rem 0;
  border: 1px solid #d7e2f0;
  border-radius: 6px;
}
article {
  margin: 1rem 0;
}
.change-columns {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}
pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 16rem;
  overflow: auto;
  padding: 0.75rem;
  background: #f5f7fb;
}
@media (max-width: 720px) {
  .change-columns {
    grid-template-columns: 1fr;
  }
}
</style>
