<script setup lang="ts">
import { onMounted, onUnmounted, watch } from "vue";
import { RouterView, useRouter } from "vue-router";
import { auth, authState } from "./auth/session";

const router = useRouter();
watch(
  () => authState.version,
  () => {
    if (!authState.user && router.currentRoute.value.meta.requiresAuth) {
      void router.replace("/login");
    }
  },
);
const onFocus = () => {
  void auth.revalidate().catch(() => {});
};
onMounted(() => window.addEventListener("focus", onFocus));
onUnmounted(() => window.removeEventListener("focus", onFocus));
</script>

<template>
  <RouterView v-slot="{ Component, route }">
    <component
      :is="Component"
      :key="route.meta.requiresAuth ? authState.version : 'public'"
    />
  </RouterView>
</template>
