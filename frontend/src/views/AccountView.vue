<script setup lang="ts">
import { ref } from "vue";
import { useRouter } from "vue-router";
import { auth, authState } from "../auth/session";

const current = ref(""),
  next = ref(""),
  confirm = ref(""),
  busy = ref(false),
  message = ref("");
const router = useRouter();
async function submit() {
  if (busy.value) return;
  if (next.value !== confirm.value) {
    message.value = "两次新密码不一致";
    return;
  }
  busy.value = true;
  message.value = "";
  try {
    await auth.changePassword(current.value, next.value);
    await router.replace("/login?reason=password");
  } catch (error) {
    message.value = error instanceof Error ? error.message : "修改未完成";
  } finally {
    current.value = "";
    next.value = "";
    confirm.value = "";
    busy.value = false;
  }
}
</script>

<template>
  <section class="status-page">
    <div class="page-heading">
      <div>
        <p class="eyebrow">MY ACCOUNT</p>
        <h1>账号设置</h1>
        <p>
          {{ authState.user?.display_name }} · {{ authState.user?.username }}
        </p>
      </div>
    </div>
    <form class="panel password-form" @submit.prevent="submit">
      <h2>修改密码</h2>
      <p class="form-description">
        修改后，此账号的所有登录会话都会失效，请重新登录。
      </p>
      <p v-if="message" role="alert" class="notice notice-warning">
        {{ message }}
      </p>
      <label
        >当前密码<input
          v-model="current"
          type="password"
          autocomplete="current-password"
          maxlength="128"
          required /></label
      ><label
        >新密码<input
          v-model="next"
          type="password"
          autocomplete="new-password"
          minlength="12"
          maxlength="128"
          required /></label
      ><label
        >确认新密码<input
          v-model="confirm"
          type="password"
          autocomplete="new-password"
          minlength="12"
          maxlength="128"
          required /></label
      ><button class="button button-dark" :disabled="busy">
        {{ busy ? "正在保存…" : "保存并重新登录" }}
      </button>
    </form>
  </section>
</template>
