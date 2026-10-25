<script setup lang="ts">
import { RouterLink, RouterView, useRouter } from "vue-router";
import { auth, authState } from "../auth/session";
const router = useRouter();
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
