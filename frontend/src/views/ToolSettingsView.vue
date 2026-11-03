<script setup lang="ts">
import { requestId } from "../api/request-id";
import { onMounted, onUnmounted, ref } from "vue";
import { onBeforeRouteLeave } from "vue-router";
import { auth } from "../auth/session";
import { ApiError } from "../auth/client";

const enabled = ref(false),
  version = ref(0),
  loading = ref(true),
  saving = ref(false),
  notice = ref(""),
  pending = ref<string | null>(null);
const controller = new AbortController();
let disposed = false;
async function load() {
  loading.value = true;
  try {
    const result = await auth.request<{
      web_search: { enabled: boolean; version: number };
    }>("/api/agent/tools", { signal: controller.signal });
    if (!disposed) {
      enabled.value = result.web_search.enabled;
      version.value = result.web_search.version;
    }
  } catch (error) {
    if (!disposed)
      notice.value = error instanceof Error ? error.message : "读取失败";
  } finally {
    if (!disposed) loading.value = false;
  }
}
async function toggle() {
  if (saving.value || loading.value) return;
  pending.value ||= JSON.stringify({
    request_id: requestId(),
    expected_version: version.value,
    enabled: !enabled.value,
  });
  saving.value = true;
  notice.value = "";
  try {
    const result = await auth.request<{ enabled: boolean; version: number }>(
      "/api/admin/agent/tools/web-search",
      { method: "POST", body: pending.value, signal: controller.signal },
    );
    if (!disposed) {
      enabled.value = result.enabled;
      version.value = result.version;
      pending.value = null;
      notice.value = "设置已保存";
    }
  } catch (error) {
    if (disposed) return;
    const uncertain =
      !(error instanceof ApiError) || error.status === 0 || error.status >= 500;
    if (!uncertain) {
      pending.value = null;
      await load();
    }
    notice.value =
      (error instanceof Error ? error.message : "保存失败") +
      (uncertain ? "。结果待确认，请重试原操作。" : "");
  } finally {
    if (!disposed) saving.value = false;
  }
}
onMounted(load);
onBeforeRouteLeave(
  (to) => to.path === "/login" || !(pending.value || saving.value),
);
onUnmounted(() => {
  disposed = true;
  controller.abort();
});
</script>

<template>
  <section class="admin-projects">
    <div class="page-heading">
      <div>
        <h1>Agent 工具管理</h1>
        <p>管理员决定工具是否可用，客户在对话中选择本轮是否允许调用。</p>
      </div>
    </div>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <article class="management-table-panel">
      <h2>公开资料搜索 · DuckDuckGo</h2>
      <p>
        公开行业和接口资料作为有来源的建议，不能替客户确认岗位、权限和业务规则。搜索不可用时保留信息缺口，继续沟通。
      </p>
      <p>
        当前状态：<strong>{{
          loading ? "正在读取…" : enabled ? "已开放" : "已关闭"
        }}</strong>
      </p>
      <button
        class="button button-dark"
        :disabled="loading || saving"
        @click="toggle"
      >
        {{
          saving
            ? "正在保存…"
            : pending
              ? "重试原操作"
              : enabled
                ? "关闭联网搜索"
                : "开放联网搜索"
        }}
      </button>
      <p class="project-muted">
        关闭后拒绝新的搜索调用，客户开关会在下一次工具状态同步时隐藏。等待人工的会话不占执行名额。
      </p>
    </article>
  </section>
</template>
