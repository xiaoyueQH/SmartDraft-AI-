<script setup lang="ts">
import { RouterLink, RouterView, useRouter } from "vue-router";
import { auth, authState } from "../auth/session";
const router = useRouter();
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
        <span class="brand-symbol" aria-hidden="true">s<span>f</span></span>
        <span>SupportFlow<small>服务管理工作台</small></span>
      </div>
      <p class="nav-caption">工作空间</p>
      <nav aria-label="管理导航">
        <RouterLink to="/admin/returns" class="admin-nav-item"
          >◇ <span>售后工单</span><span>↗</span></RouterLink
        >
        <RouterLink to="/admin/catalog" class="admin-nav-item"
          >▦ <span>商品管理</span><span>↗</span></RouterLink
        >
        <RouterLink to="/admin/orders" class="admin-nav-item"
          >▤ <span>订单管理</span><span>↗</span></RouterLink
        >
        <RouterLink to="/admin" class="admin-nav-item"
          >◉ <span>运行概览</span><span>↗</span></RouterLink
        >
        <RouterLink to="/admin/users" class="admin-nav-item"
          >♙ <span>账号管理</span><span>↗</span></RouterLink
        >
      </nav>
      <div class="sidebar-note">
        <span class="dot"></span>账号与订单 · 第二阶段
      </div>
    </aside>
    <div class="admin-content">
      <header class="admin-header">
        <span>服务管理工作台</span>
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
