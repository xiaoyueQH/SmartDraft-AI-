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
  <div class="customer-shell">
    <header class="customer-header">
      <RouterLink class="brand" to="/" aria-label="SupportFlow 首页">
        <span class="brand-symbol" aria-hidden="true">SF</span>
        <span>SupportFlow<small>项目协作管理系统</small></span>
      </RouterLink>
      <nav aria-label="用户导航">
        <RouterLink to="/" exact-active-class="is-active">需求沟通</RouterLink>
        <RouterLink to="/status" active-class="is-active">服务状态</RouterLink>
      </nav>
      <div class="account-actions">
        <RouterLink to="/account">{{ authState.user?.display_name }}</RouterLink
        ><RouterLink to="/login?switch=1">切换账号</RouterLink
        ><button type="button" @click="logout">退出</button>
      </div>
    </header>
    <main id="main-content" class="customer-main"><RouterView /></main>
    <footer class="customer-footer">
      <span>SupportFlow</span><span>项目需求与交付管理</span>
    </footer>
  </div>
</template>
