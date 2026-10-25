<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { auth } from "../auth/session";
import { money, dateTime, type Page } from "../api/orders";
import {
  returnStatuses,
  type ReturnSummary,
  type ReturnStatus,
} from "../api/workflow";
const props = defineProps<{ admin?: boolean }>();
const rows = ref<ReturnSummary[]>([]),
  status = ref<ReturnStatus | "">(""),
  loading = ref(false),
  message = ref("");
const offset = ref(0),
  more = ref(false),
  pageSize = 20;
let submitted = "";
const controller = new AbortController();
async function load(next = 0) {
  if (loading.value) return;
  loading.value = true;
  message.value = "";
  rows.value = [];
  more.value = false;
  try {
    const params = new URLSearchParams({
      limit: String(pageSize),
      offset: String(next),
    });
    if (submitted) params.set("status", submitted);
    const result = await auth.request<Page<ReturnSummary>>(
      `${props.admin ? "/api/admin/returns" : "/api/returns"}?${params}`,
      { signal: controller.signal },
    );
    if (controller.signal.aborted) return;
    rows.value = result.items;
    more.value = result.has_more;
    offset.value = next;
  } catch (error) {
    if (!controller.signal.aborted)
      message.value = error instanceof Error ? error.message : "读取失败";
  } finally {
    loading.value = false;
  }
}
function search() {
  submitted = status.value;
  void load();
}
onMounted(() => load());
onUnmounted(() => controller.abort());
</script>
<template>
  <section class="status-page">
    <div class="page-heading">
      <div>
        <p class="eyebrow">AFTER-SALES WORKSPACE</p>
        <h1>{{ admin ? "售后工单" : "我的售后" }}</h1>
        <p>从申请到审核、退货、验收与退款，进度清楚可见。</p>
      </div>
    </div>
    <form class="order-filters panel" @submit.prevent="search">
      <label
        >工单状态<select v-model="status" :disabled="loading">
          <option value="">全部状态</option>
          <option v-for="(name, key) in returnStatuses" :key="key" :value="key">
            {{ name }}
          </option>
        </select></label
      ><button class="button button-dark" :disabled="loading">查询工单</button>
    </form>
    <p v-if="message" role="alert" class="notice notice-warning">
      {{ message }}
    </p>
    <div class="panel table-panel" :aria-busy="loading">
      <div class="table-scroll">
        <table class="accounts-table orders-table">
          <thead>
            <tr>
              <th>订单号</th>
              <th v-if="admin">用户</th>
              <th>状态</th>
              <th>申请原因</th>
              <th>退款金额</th>
              <th>申请时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id">
              <td>{{ row.number }}</td>
              <td v-if="admin">{{ row.customer_username }}</td>
              <td>
                <span class="badge badge-neutral">{{
                  returnStatuses[row.status]
                }}</span>
              </td>
              <td class="return-reason">{{ row.reason }}</td>
              <td>{{ money(row.refund_cents) }}</td>
              <td>{{ dateTime(row.created_at) }}</td>
              <td>
                <RouterLink
                  class="text-button"
                  :to="{
                    path: admin ? '/admin/orders' : '/orders',
                    query: { order_id: row.order_id },
                  }"
                  >查看处理 ↗</RouterLink
                >
              </td>
            </tr>
            <tr v-if="!rows.length">
              <td :colspan="admin ? 7 : 6">
                {{ loading ? "正在读取…" : "暂无符合条件的工单" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <button
          class="button button-outline"
          :disabled="loading || offset === 0"
          @click="load(offset - pageSize)"
        >
          上一页</button
        ><span>第 {{ offset / pageSize + 1 }} 页</span
        ><button
          class="button button-outline"
          :disabled="loading || !more || offset + pageSize > 10000"
          @click="load(offset + pageSize)"
        >
          下一页
        </button>
      </div>
    </div>
  </section>
</template>
<style scoped>
.return-reason {
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
