<script setup lang="ts">
import { onMounted, ref } from "vue";
import { auth } from "../auth/session";
import type { User } from "../auth/client";

const users = ref<User[]>([]),
  loading = ref(false),
  saving = ref(false),
  message = ref("");
const username = ref(""),
  displayName = ref(""),
  password = ref(""),
  role = ref<User["role"]>("user");
const offset = ref(0),
  pageSize = 20;
const labels = { admin: "管理员", staff: "项目成员", user: "客户" };
async function load(nextOffset = offset.value) {
  if (loading.value) return;
  loading.value = true;
  message.value = "";
  try {
    users.value = await auth.request<User[]>(
      `/api/admin/users?limit=${pageSize}&offset=${nextOffset}`,
    );
    offset.value = nextOffset;
  } catch (error) {
    message.value = error instanceof Error ? error.message : "读取失败";
  } finally {
    loading.value = false;
  }
}
async function create() {
  if (saving.value) return;
  saving.value = true;
  message.value = "";
  try {
    await auth.request("/api/admin/users", {
      method: "POST",
      body: JSON.stringify({
        username: username.value,
        display_name: displayName.value,
        password: password.value,
        role: role.value,
      }),
    });
    username.value = "";
    displayName.value = "";
    password.value = "";
    await load(0);
  } catch (error) {
    message.value = error instanceof Error ? error.message : "创建失败";
  } finally {
    saving.value = false;
    password.value = "";
  }
}
onMounted(() => load());
</script>

<template>
  <section class="status-page">
    <div class="page-heading">
      <div>
        <p class="eyebrow">ACCOUNT MANAGEMENT</p>
        <h1>账号管理</h1>
        <p>开通账号，并为每个人分配合适的角色。</p>
      </div>
      <button class="button button-outline" :disabled="loading" @click="load()">
        刷新列表 ↻
      </button>
    </div>
    <p v-if="message" role="alert" class="notice notice-warning">
      {{ message }}
    </p>
    <div class="panel">
      <div class="section-heading"><h2>开通账号</h2></div>
      <form class="account-create-form" @submit.prevent="create">
        <label
          >账号<input
            v-model="username"
            autocomplete="off"
            required
            minlength="3"
            maxlength="32"
            pattern="[a-zA-Z0-9_][a-zA-Z0-9_.-]{2,31}"
            placeholder="3～32 位字母、数字或 _ . -" /></label
        ><label
          >显示名称<input
            v-model="displayName"
            required
            maxlength="64" /></label
        ><label
          >角色<select v-model="role">
            <option value="user">客户</option>
            <option value="staff">项目成员</option>
            <option value="admin">管理员</option>
          </select></label
        ><label
          >初始密码<input
            v-model="password"
            type="password"
            autocomplete="new-password"
            required
            minlength="12"
            maxlength="128"
            placeholder="至少 12 个字符" /></label
        ><button class="button button-dark" :disabled="saving">
          {{ saving ? "正在创建…" : "创建账号" }}
        </button>
      </form>
    </div>
    <div class="panel table-panel">
      <div class="section-heading">
        <h2>账号列表</h2>
        <span>{{
          loading ? "正在读取…" : `第 ${offset / pageSize + 1} 页`
        }}</span>
      </div>
      <div class="table-scroll">
        <table class="accounts-table">
          <thead>
            <tr>
              <th>账号</th>
              <th>显示名称</th>
              <th>角色</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="user in users" :key="user.id">
              <td>{{ user.username }}</td>
              <td>{{ user.display_name }}</td>
              <td>{{ labels[user.role] }}</td>
              <td>
                <span
                  class="badge"
                  :class="user.is_active ? 'badge-good' : 'badge-neutral'"
                  >{{ user.is_active ? "启用" : "停用" }}</span
                >
              </td>
            </tr>
            <tr v-if="!users.length && !loading">
              <td colspan="4">暂无账号</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <button
          class="button button-outline"
          :disabled="loading || offset === 0"
          @click="load(Math.max(0, offset - pageSize))"
        >
          上一页</button
        ><button
          class="button button-outline"
          :disabled="loading || users.length < pageSize"
          @click="load(offset + pageSize)"
        >
          下一页
        </button>
      </div>
    </div>
  </section>
</template>
