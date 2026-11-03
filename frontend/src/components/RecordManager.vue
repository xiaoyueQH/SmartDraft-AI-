<script setup lang="ts">
import { requestId } from "../api/request-id";
import { computed, onUnmounted, ref, watch } from "vue";
import { auth } from "../auth/session";
import { ApiError } from "../auth/client";
import type { Project } from "../api/projects";

const props = defineProps<{ admin: boolean }>();
const emit = defineEmits<{
  changed: [items: Project[]];
  busy: [value: boolean];
}>();
const dialog = ref<HTMLDialogElement | null>(null);
const rows = ref<Project[]>([]),
  selected = ref<string[]>([]),
  offset = ref(0),
  more = ref(false);
const deleted = ref(false),
  search = ref(""),
  appliedSearch = ref(""),
  loading = ref(false),
  saving = ref(false),
  message = ref("");
const mode = ref<"list" | "confirm" | "rename">("list"),
  targets = ref<Project[]>([]),
  title = ref(""),
  acknowledged = ref(false);
const pending = ref<string | null>(null);
const controller = new AbortController();
let disposed = false,
  generation = 0,
  trigger: HTMLElement | null = null;
const canManage = (row: Project) => props.admin || row.phase === "discovery";
const selectable = computed(() => rows.value.filter(canManage));
const allSelected = computed(
  () =>
    selectable.value.length > 0 &&
    selectable.value.every((row) => selected.value.includes(row.id)),
);
const locked = computed(() => saving.value || !!pending.value);
const visibleTitle = computed(() =>
  /[\p{L}\p{N}\p{P}\p{S}]/u.test(title.value),
);
watch(locked, (value) => emit("busy", value), { flush: "sync" });
function describe(error: unknown) {
  return error instanceof Error ? error.message : "操作未完成，请稍后重试";
}

async function load(page = offset.value) {
  if (locked.value) return;
  const current = ++generation;
  loading.value = true;
  message.value = "";
  selected.value = [];
  try {
    const params = new URLSearchParams({
      offset: String(page),
      limit: "20",
      deleted: String(deleted.value),
      q: appliedSearch.value,
    });
    const result = await auth.request<{ items: Project[]; has_more: boolean }>(
      `/api/projects?${params}`,
      { signal: controller.signal },
    );
    if (disposed || current !== generation) return;
    rows.value = result.items;
    more.value = result.has_more;
    offset.value = page;
    if (!rows.value.length && page > 0) await load(0);
  } catch (error) {
    if (!disposed && current === generation) {
      rows.value = [];
      more.value = false;
      message.value = describe(error);
    }
  } finally {
    if (!disposed && current === generation) loading.value = false;
  }
}
function open() {
  if (dialog.value?.open) return;
  trigger =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
  mode.value = "list";
  message.value = "";
  dialog.value?.showModal();
  void load(0);
}
function close() {
  if (locked.value) return;
  generation++;
  loading.value = false;
  dialog.value?.close();
  trigger?.focus({ preventScroll: true });
}
function switchTab(value: boolean) {
  if (locked.value || loading.value) return;
  deleted.value = value;
  mode.value = "list";
  void load(0);
}
function toggleAll() {
  selected.value = allSelected.value
    ? []
    : selectable.value.map((row) => row.id);
}
function prepare(action: "rename" | "confirm", row?: Project) {
  if (locked.value || loading.value) return;
  targets.value = row
    ? [row]
    : rows.value.filter((item) => selected.value.includes(item.id));
  if (!targets.value.length || targets.value.some((item) => !canManage(item)))
    return;
  mode.value = action;
  acknowledged.value = false;
  title.value = targets.value[0]?.title || "";
  message.value = "";
}
async function submit() {
  if (
    locked.value ||
    !targets.value.length ||
    (mode.value === "rename" && !visibleTitle.value) ||
    (mode.value === "confirm" && !acknowledged.value)
  )
    return;
  pending.value = JSON.stringify({
    request_id: requestId(),
    action:
      mode.value === "rename" ? "rename" : deleted.value ? "restore" : "trash",
    title: mode.value === "rename" ? title.value.trim() : "",
    targets: targets.value.map((row) => ({
      id: row.id,
      expected_version: row.version,
    })),
  });
  await perform();
}
async function perform() {
  if (!pending.value || saving.value) return;
  saving.value = true;
  message.value = "";
  let result: { items: Project[] } | null = null;
  try {
    result = await auth.request<{ items: Project[] }>(
      "/api/projects/records/actions",
      { method: "POST", body: pending.value, signal: controller.signal },
    );
    if (disposed) return;
    pending.value = null;
  } catch (error) {
    if (disposed) return;
    const uncertain =
      !(error instanceof ApiError) || error.status === 0 || error.status >= 500;
    if (!uncertain) pending.value = null;
    message.value =
      describe(error) + (uncertain ? "。结果待确认，请重试原操作。" : "");
  } finally {
    if (!disposed) saving.value = false;
  }
  if (disposed || !result) return;
  emit("changed", result.items);
  mode.value = "list";
  targets.value = [];
  selected.value = [];
  await load();
}
async function back() {
  if (locked.value) return;
  mode.value = "list";
  await load();
}
defineExpose({ open });
onUnmounted(() => {
  disposed = true;
  generation++;
  controller.abort();
});
</script>

<template>
  <dialog
    ref="dialog"
    class="project-dialog record-manager"
    @cancel.prevent="close"
  >
    <div class="project-dialog-heading">
      <div>
        <h2>记录管理</h2>
        <p>管理沟通记录与项目；回收站中的记录可以恢复。</p>
      </div>
      <button class="text-button" :disabled="locked" @click="close">
        关闭
      </button>
    </div>
    <p v-if="message" class="notice notice-warning" role="alert">
      {{ message }}
      <button
        v-if="pending"
        class="text-button"
        :disabled="saving"
        @click="perform"
      >
        重试原操作
      </button>
    </p>
    <template v-if="mode === 'list'">
      <nav class="project-tabs" aria-label="记录分类">
        <button
          :class="{ active: !deleted }"
          :disabled="locked || loading"
          @click="switchTab(false)"
        >
          在用记录</button
        ><button
          :class="{ active: deleted }"
          :disabled="locked || loading"
          @click="switchTab(true)"
        >
          回收站
        </button>
      </nav>
      <form
        class="record-search"
        @submit.prevent="
          appliedSearch = search.trim();
          load(0);
        "
      >
        <label for="record-search"
          >记录名称<input
            id="record-search"
            v-model="search"
            maxlength="80"
            :disabled="locked || loading"
            placeholder="按名称搜索" /></label
        ><button class="button button-outline" :disabled="locked || loading">
          查询</button
        ><button
          type="button"
          class="button button-outline"
          :disabled="locked || loading || !selected.length"
          @click="prepare('confirm')"
        >
          {{ deleted ? "恢复所选" : "删除所选" }}（{{ selected.length }}）
        </button>
      </form>
      <p v-if="!admin" class="project-muted">
        您可以管理自己的沟通记录；正式项目由负责人管理。
      </p>
      <div class="table-scroll record-table">
        <table class="accounts-table">
          <thead>
            <tr>
              <th>
                <input
                  type="checkbox"
                  aria-label="全选本页可管理记录"
                  :checked="allSelected"
                  :disabled="locked || loading || !selectable.length"
                  @change="toggleAll"
                />
              </th>
              <th>名称 / 编号</th>
              <th>类型</th>
              <th>{{ deleted ? "删除时间" : "创建时间" }}</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id">
              <td>
                <input
                  v-model="selected"
                  type="checkbox"
                  :value="row.id"
                  :aria-label="`选择记录 ${row.id}`"
                  :disabled="locked || loading || !canManage(row)"
                />
              </td>
              <td>
                <strong>{{ row.title }}</strong
                ><small>{{ row.id.slice(-8) }}</small>
              </td>
              <td>
                {{
                  row.phase === "discovery"
                    ? "需求沟通"
                    : `正式项目 · V${row.revision}`
                }}
              </td>
              <td>
                {{
                  new Date(
                    (deleted ? row.deleted_at : row.created_at) ||
                      row.created_at,
                  ).toLocaleString("zh-CN")
                }}
              </td>
              <td>
                <div v-if="canManage(row)" class="record-row-actions">
                  <button
                    v-if="!deleted"
                    class="text-button"
                    :disabled="locked || loading"
                    @click="prepare('rename', row)"
                  >
                    重命名</button
                  ><button
                    class="text-button"
                    :class="{ 'danger-text': !deleted }"
                    :disabled="locked || loading"
                    @click="prepare('confirm', row)"
                  >
                    {{ deleted ? "恢复" : "删除" }}
                  </button>
                </div>
                <span v-else class="project-muted">负责人管理</span>
              </td>
            </tr>
            <tr v-if="!rows.length">
              <td colspan="5">
                {{
                  loading ? "正在读取…" : deleted ? "回收站为空" : "暂无记录"
                }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <button
          class="button button-outline"
          :disabled="locked || loading || offset === 0"
          @click="load(Math.max(0, offset - 20))"
        >
          上一页</button
        ><button
          class="button button-outline"
          :disabled="locked || loading || !more"
          @click="load(offset + 20)"
        >
          下一页
        </button>
      </div>
    </template>
    <form v-else @submit.prevent="submit">
      <p v-if="mode === 'confirm'">
        确认将以下 {{ targets.length }} 条记录{{
          deleted ? "恢复到工作列表" : "移入回收站"
        }}？{{
          deleted ? "" : "对话、需求版本和处理历史会保留，可在回收站恢复。"
        }}
      </p>
      <ul class="record-targets">
        <li v-for="row in targets" :key="row.id">
          {{ row.title }} <small>· {{ row.id.slice(-8) }}</small>
        </li>
      </ul>
      <label v-if="mode === 'rename'"
        >记录名称<input
          v-model="title"
          required
          maxlength="120"
          :disabled="locked"
      /></label>
      <label v-else class="record-ack"
        ><input
          v-model="acknowledged"
          type="checkbox"
          :disabled="locked"
        />我已核对选中记录，确认{{ deleted ? "恢复" : "移入回收站" }}</label
      >
      <p v-if="mode === 'rename' && !visibleTitle" role="status">
        记录名称需要包含可见文字或符号。
      </p>
      <div class="project-actions">
        <button
          type="button"
          class="button button-outline"
          :disabled="locked"
          @click="back"
        >
          返回列表</button
        ><button
          class="button button-dark"
          :disabled="
            locked || (mode === 'rename' ? !visibleTitle : !acknowledged)
          "
        >
          {{
            saving
              ? "处理中…"
              : mode === "rename"
                ? "保存名称"
                : deleted
                  ? "确认恢复"
                  : "确认删除"
          }}
        </button>
      </div>
    </form>
  </dialog>
</template>
