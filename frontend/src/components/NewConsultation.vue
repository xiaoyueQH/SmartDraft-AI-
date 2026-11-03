<script setup lang="ts">
import { requestId } from "../api/request-id";
import { ref, onUnmounted } from "vue";
import { auth } from "../auth/session";
import { ApiError, type User } from "../auth/client";
import type { Project } from "../api/projects";

const emit = defineEmits<{ created: [project: Project] }>();
const dialog = ref<HTMLDialogElement | null>(null),
  customers = ref<User[]>([]),
  owner = ref(""),
  notice = ref(""),
  loading = ref(false),
  saving = ref(false),
  pending = ref<string | null>(null);
const controller = new AbortController();
let disposed = false;
async function open() {
  dialog.value?.showModal();
  loading.value = true;
  notice.value = "";
  try {
    const users = await auth.request<User[]>("/api/admin/users?limit=100", {
      signal: controller.signal,
    });
    if (!disposed)
      customers.value = users.filter(
        (user) => user.role !== "admin" && user.is_active,
      );
  } catch (error) {
    if (!disposed)
      notice.value =
        error instanceof Error ? error.message : "客户列表读取失败";
  } finally {
    if (!disposed) loading.value = false;
  }
}
function close() {
  if (!saving.value && !pending.value) dialog.value?.close();
}
async function submit() {
  if (saving.value || loading.value || !owner.value) return;
  pending.value ||= JSON.stringify({
    request_id: requestId(),
    owner_id: owner.value,
  });
  saving.value = true;
  try {
    const created = await auth.request<Project>("/api/consultations", {
      method: "POST",
      body: pending.value,
      signal: controller.signal,
    });
    if (disposed) return;
    pending.value = null;
    dialog.value?.close();
    emit("created", created);
  } catch (error) {
    if (disposed) return;
    const uncertain =
      !(error instanceof ApiError) || error.status === 0 || error.status >= 500;
    if (!uncertain) pending.value = null;
    notice.value =
      (error instanceof Error ? error.message : "创建失败") +
      (uncertain ? "。结果待确认，请重试原操作。" : "");
  } finally {
    if (!disposed) saving.value = false;
  }
}
defineExpose({ open, busy: () => saving.value || !!pending.value });
onUnmounted(() => {
  disposed = true;
  controller.abort();
});
</script>

<template>
  <dialog ref="dialog" class="project-dialog" @cancel.prevent="close">
    <div class="project-dialog-heading">
      <h2>发起客户沟通</h2>
      <button
        class="text-button"
        :disabled="saving || !!pending"
        @click="close"
      >
        关闭
      </button>
    </div>
    <p>选择已有客户账号，先了解业务，确认需求后再立项。</p>
    <p v-if="notice" class="notice notice-warning" role="alert">{{ notice }}</p>
    <form @submit.prevent="submit">
      <label
        >客户账号<select
          v-model="owner"
          required
          :disabled="loading || saving || !!pending"
        >
          <option disabled value="">
            {{ loading ? "正在读取…" : "请选择客户" }}
          </option>
          <option
            v-for="customer in customers"
            :key="customer.id"
            :value="customer.id"
          >
            {{ customer.display_name }}（{{ customer.username }}）
          </option>
        </select></label
      >
      <div class="project-actions">
        <button
          class="button button-dark"
          :disabled="loading || saving || !owner"
        >
          {{ saving ? "正在创建…" : pending ? "重试原操作" : "开始需求沟通" }}
        </button>
      </div>
    </form>
  </dialog>
</template>
