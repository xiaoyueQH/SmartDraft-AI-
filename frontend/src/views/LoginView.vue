<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { auth } from "../auth/session";
import type { SavedLogin, User } from "../auth/client";

const route = useRoute(),
  router = useRouter();
const username = ref(""),
  password = ref(""),
  busy = ref(false),
  message = ref("");
const savedLogins = ref<SavedLogin[]>([]);
onMounted(async () => {
  if (route.query.switch === "1") auth.prepareSwitch();
  try {
    savedLogins.value = await auth.availableSessions();
  } catch {
    /* Password login remains available if the account chooser cannot load. */
  }
});

async function enter(user: User) {
  const next = typeof route.query.next === "string" ? route.query.next : "";
  const safeNext =
    next.startsWith("/") &&
    !next.startsWith("//") &&
    !next.startsWith("/login");
  await router.replace(
    safeNext ? next : user.role === "admin" ? "/admin" : "/",
  );
}

async function resume(saved: SavedLogin) {
  if (busy.value) return;
  busy.value = true;
  message.value = "";
  try {
    await enter(await auth.resume(saved.session_id));
  } catch (error) {
    message.value = error instanceof Error ? error.message : "恢复登录未完成";
  } finally {
    busy.value = false;
  }
}

async function submit() {
  if (busy.value) return;
  busy.value = true;
  message.value = "";
  try {
    const user = await auth.login(username.value, password.value);
    await enter(user);
  } catch (error) {
    message.value =
      error instanceof Error ? error.message : "登录未完成，请重试";
  } finally {
    password.value = "";
    busy.value = false;
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-story">
      <div class="brand brand-light">
        <span class="brand-symbol">SF</span
        ><span>SupportFlow<small>项目协作管理系统</small></span>
      </div>
      <div>
        <h1>项目协作管理系统</h1>
        <p>需求管理 · 变更评审 · 交付验收 · 售后工单</p>
      </div>
      <small>每个标签页，可以独立登录不同账号。</small>
    </section>
    <section class="login-form-panel">
      <form class="login-form" @submit.prevent="submit">
        <h2>{{ route.query.switch ? "登录另一个账号" : "账号登录" }}</h2>
        <p class="form-description">使用管理员为你开通的账号登录。</p>
        <p v-if="message" role="alert" class="notice notice-warning">
          {{ message }}
        </p>
        <div v-if="savedLogins.length" class="saved-logins">
          <p>继续已保持的登录</p>
          <button
            v-for="saved in savedLogins"
            :key="saved.session_id"
            type="button"
            :disabled="busy"
            @click="resume(saved)"
          >
            <span
              >{{ saved.user.display_name
              }}<small>{{ saved.user.username }}</small></span
            ><span>继续</span></button
          ><span class="chooser-divider">或输入其他账号</span>
        </div>
        <label for="username">账号</label
        ><input
          id="username"
          v-model="username"
          name="username"
          autocomplete="username"
          maxlength="32"
          required
          :disabled="busy"
          placeholder="请输入账号"
        /><label for="password">密码</label
        ><input
          id="password"
          v-model="password"
          name="password"
          type="password"
          autocomplete="current-password"
          maxlength="128"
          required
          :disabled="busy"
          placeholder="请输入密码"
        /><button class="button button-dark" type="submit" :disabled="busy">
          {{ busy ? "正在登录…" : "登录" }}
        </button>
        <p class="login-help">
          保持登录，到期自动续期。退出只影响当前登录会话。
        </p>
        <p
          v-if="route.query.reason === 'password'"
          class="notice"
          role="status"
        >
          密码已更新，请重新登录。
        </p>
        <p
          v-if="route.query.reason === 'logout-unconfirmed'"
          class="notice notice-warning"
          role="alert"
        >
          本地已退出，服务端退出尚未确认。请恢复连接后重新登录并退出。
        </p>
        <p
          v-if="route.query.reason === 'connection'"
          class="notice notice-warning"
        >
          暂时无法恢复登录，请确认服务连接后重试。
        </p>
      </form>
    </section>
  </main>
</template>
