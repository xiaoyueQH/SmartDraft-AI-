<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { RouterLink, RouterView, useRouter } from "vue-router";
import { auth, authState } from "../auth/session";
import {
  supportReasonLabels,
  type SupportInbox,
  type SupportRequest,
} from "../api/projects";
const router = useRouter();
const supportRequests = ref<SupportRequest[]>([]);
const supportTotal = ref(0),
  supportError = ref(false);
const requestMenu = ref<HTMLDetailsElement | null>(null);
function requestReasons(item: SupportRequest) {
  return (
    item.reasons?.map((reason) => supportReasonLabels[reason]).join(" · ") ||
    "等待客服接入"
  );
}
let supportTimer: ReturnType<typeof setTimeout> | null = null,
  disposed = false;
async function loadSupportRequests() {
  const account = authState.user?.id;
  try {
    const result = await auth.request<SupportInbox>(
      "/api/conversations/requests",
    );
    if (!disposed && account === authState.user?.id) {
      supportRequests.value = Array.isArray(result.items) ? result.items : [];
      supportTotal.value = result.counts?.total ?? supportRequests.value.length;
      supportError.value = false;
    }
  } catch {
    if (!disposed && account === authState.user?.id) supportError.value = true;
  } finally {
    if (!disposed) supportTimer = setTimeout(loadSupportRequests, 5000);
  }
}
onMounted(() => {
  void loadSupportRequests();
});
onUnmounted(() => {
  disposed = true;
  if (supportTimer) clearTimeout(supportTimer);
});
const businessLinks = [
  { path: "/admin", title: "管理工作台" },
  { path: "/admin/projects", title: "客户与项目" },
  { path: "/admin/reviews", title: "需求审核" },
  { path: "/admin/tasks", title: "开发任务" },
  { path: "/admin/deliveries", title: "交付验收" },
  { path: "/admin/service", title: "售后工单" },
  { path: "/admin/tools", title: "工具管理" },
];
async function logout() {
  try {
    await auth.logout();
    await router.replace("/login");
  } catch {
    await router.replace("/login?reason=logout-unconfirmed");
  }
}
</script>

<template>
  <div class="admin-shell">
    <aside class="admin-sidebar">
      <div class="brand brand-light">
        <span class="brand-symbol" aria-hidden="true">SF</span>
        <span>SupportFlow<small>项目协作管理系统</small></span>
      </div>
      <p class="nav-caption">业务管理</p>
      <nav aria-label="管理导航">
        <RouterLink
          v-for="link in businessLinks"
          :key="link.path"
          :to="link.path"
          class="admin-nav-item"
          ><svg
            class="nav-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            aria-hidden="true"
          >
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <path d="M3 10h18M9 10v10" /></svg
          ><span>{{ link.title }}</span></RouterLink
        >
        <RouterLink to="/admin/status" class="admin-nav-item"
          ><svg
            class="nav-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            aria-hidden="true"
          >
            <path d="M3 12h4l3-7 4 14 3-7h4" /></svg
          ><span>运行状态</span></RouterLink
        >
        <RouterLink to="/admin/users" class="admin-nav-item"
          ><svg
            class="nav-icon"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            aria-hidden="true"
          >
            <circle cx="12" cy="8" r="4" />
            <path d="M4 21v-2a8 8 0 0 1 16 0v2" /></svg
          ><span>账号管理</span></RouterLink
        >
      </nav>
      <div class="sidebar-note">SupportFlow 项目管理</div>
    </aside>
    <div class="admin-content">
      <header class="admin-header">
        <span>项目协作管理系统</span>
        <div class="account-actions">
          <details ref="requestMenu" class="support-notifications">
            <summary>
              接待与异常
              <span v-if="supportTotal">{{ supportTotal }}</span>
            </summary>
            <div class="support-request-list">
              <p v-if="supportError" role="status">
                接待列表暂未更新，请在工作台重新查询。
              </p>
              <p v-else-if="!supportRequests.length">暂无待处理接待。</p>
              <RouterLink
                v-for="item in supportRequests"
                :key="item.id"
                :to="`/admin/projects/${item.id}`"
                @click="requestMenu && (requestMenu.open = false)"
                >{{ item.customer_name }} · {{ item.title
                }}<small>{{ requestReasons(item) }}</small></RouterLink
              >
              <RouterLink
                to="/admin"
                @click="requestMenu && (requestMenu.open = false)"
                >查看全部待处理接待 →</RouterLink
              >
            </div>
          </details>
          <RouterLink to="/account">{{
            authState.user?.display_name
          }}</RouterLink
          ><RouterLink to="/login?switch=1">切换账号</RouterLink
          ><button type="button" @click="logout">退出</button>
        </div>
      </header>
      <main id="main-content" class="admin-main"><RouterView /></main>
    </div>
  </div>
</template>

<style scoped>
.support-notifications {
  position: relative;
}
.support-notifications summary {
  cursor: pointer;
  color: #245c9c;
}
.support-request-list {
  position: absolute;
  right: 0;
  top: 28px;
  z-index: 30;
  width: 320px;
  max-height: 400px;
  overflow: auto;
  padding: 12px;
  background: white;
  border: 1px solid #dbe3ee;
  border-radius: 6px;
  box-shadow: 0 6px 20px #182b4520;
}
.support-request-list a {
  display: block;
  padding: 10px 6px;
  border-bottom: 1px solid #edf1f6;
}
.support-request-list small {
  display: block;
  margin-top: 4px;
  color: #64748b;
}
</style>
