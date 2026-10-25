<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";

import { checkHealth, type HealthSnapshot } from "../api/health";

const route = useRoute();
const admin = computed(() => route.path.startsWith("/admin"));
const snapshot = ref<HealthSnapshot | null>(null);
const loading = ref(false);
const message = ref("");
let controller: AbortController | null = null;
let disposed = false;
const allReady = computed(
  () => snapshot.value?.api === "ok" && snapshot.value?.database === "ok",
);
const updatedAt = computed(
  () =>
    snapshot.value?.checkedAt.toLocaleTimeString("zh-CN", { hour12: false }) ||
    "—",
);

async function refresh() {
  if (loading.value) return;
  loading.value = true;
  snapshot.value = null;
  message.value = "";
  const current = new AbortController();
  controller = current;
  const timeout = window.setTimeout(() => current.abort(), 8000);
  try {
    const next = await checkHealth(current.signal);
    if (!disposed) snapshot.value = next;
  } catch {
    if (!disposed) message.value = "检查未完成，请稍后重试。";
  } finally {
    window.clearTimeout(timeout);
    if (controller === current) controller = null;
    if (!disposed) loading.value = false;
  }
}

onMounted(refresh);
onUnmounted(() => {
  disposed = true;
  controller?.abort();
});
</script>

<template>
  <section class="status-page">
    <div class="page-heading">
      <div>
        <h1>{{ admin ? "运行状态" : "服务状态" }}</h1>
        <p>查看服务当前的连接情况。</p>
      </div>
      <button
        class="button button-outline"
        type="button"
        :disabled="loading"
        @click="refresh"
      >
        {{ loading ? "正在检查…" : "重新检查" }}
        <span aria-hidden="true">↻</span>
      </button>
    </div>
    <p v-if="message" class="notice notice-warning" role="alert">
      {{ message }}
    </p>
    <div
      class="status-banner"
      :class="{ 'status-ready': allReady }"
      role="status"
      aria-live="polite"
    >
      <span class="status-emblem" aria-hidden="true">{{
        loading ? "◌" : allReady ? "✓" : "·"
      }}</span>
      <div>
        <h2>
          {{
            loading
              ? "正在连接服务"
              : allReady
                ? "基础服务已就绪"
                : snapshot
                  ? "部分服务尚未就绪"
                  : "等待检查"
          }}
        </h2>
        <p>
          {{
            allReady
              ? "接口与数据连接正常。"
              : "各项连接状态如下，可稍后重新检查。"
          }}
        </p>
      </div>
      <span class="last-checked">检查时间 {{ updatedAt }}</span>
    </div>
    <div class="health-grid">
      <article class="health-card">
        <div>
          <span class="health-icon">↔</span
          ><span
            class="badge"
            :class="snapshot?.api === 'ok' ? 'badge-good' : 'badge-neutral'"
            >{{
              !snapshot ? "待检查" : snapshot.api === "ok" ? "正常" : "未连接"
            }}</span
          >
        </div>
        <h3>应用服务</h3>
        <p>
          {{
            snapshot?.api === "ok" ? "可以接收并响应请求" : "等待应用服务响应"
          }}
        </p>
      </article>
      <article class="health-card">
        <div>
          <span class="health-icon">▤</span
          ><span
            class="badge"
            :class="
              snapshot?.database === 'ok' ? 'badge-good' : 'badge-neutral'
            "
            >{{
              !snapshot
                ? "待检查"
                : snapshot.database === "ok"
                  ? "正常"
                  : snapshot.database === "unavailable"
                    ? "未就绪"
                    : "未知"
            }}</span
          >
        </div>
        <h3>数据服务</h3>
        <p>
          {{
            snapshot?.database === "ok"
              ? "数据连接检查通过"
              : "等待数据服务连接"
          }}
        </p>
      </article>
    </div>
    <div v-if="admin && snapshot?.requestId" class="panel">
      <div class="section-heading"><h2>检查信息</h2></div>
      <p class="request-id">请求编号 {{ snapshot.requestId }}</p>
    </div>
  </section>
</template>
