<script setup lang="ts">
import { RouterLink } from "vue-router";
</script>

<template>
  <main class="not-found">
    <p class="eyebrow">404 / PAGE NOT FOUND</p>
    <h1>这个页面暂时找不到。</h1>
    <p>可以返回服务首页继续查看。</p>
    <RouterLink class="button button-dark" to="/">返回首页 ↗</RouterLink>
  </main>
</template>
