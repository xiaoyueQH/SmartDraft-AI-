<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute } from "vue-router";
import { auth } from "../auth/session";
import OrderBusinessPanel from "../components/OrderBusinessPanel.vue";
import type { WorkflowReceipt } from "../api/workflow";
import {
  dateTime,
  money,
  productText,
  orderStatuses,
  type OrderDetail,
  type OrderStatus,
  type OrderSummary,
  type CancellationReceipt,
  type Page,
} from "../api/orders";
const props = defineProps<{ admin?: boolean }>();
const route = useRoute();
const writeBusy = ref(false),
  detailNotice = ref("");
function protectPendingOperation() {
  if (writeBusy.value && auth.user) {
    detailNotice.value = "当前操作尚未完成，请先完成、返回或重试后再离开";
    return false;
  }
  return true;
}
onBeforeRouteLeave(protectPendingOperation);
onBeforeRouteUpdate(protectPendingOperation);
const orders = ref<OrderSummary[]>([]),
  selected = ref<OrderDetail | null>(null);
const loading = ref(false),
  loadingDetail = ref(false),
  message = ref(""),
  detailMessage = ref("");
const status = ref<OrderStatus | "">(""),
  number = ref(""),
  offset = ref(0),
  hasMore = ref(false),
  pageSize = 20;
const listController = new AbortController();
let detailController: AbortController | null = null;
const endpoint = () => (props.admin ? "/api/admin/orders" : "/api/orders");
let filters = { status: "", number: "" };
async function search() {
  if (loading.value || writeBusy.value) return;
  filters = { status: status.value, number: number.value.trim() };
  await load(0);
}
async function load(nextOffset = offset.value) {
  if (loading.value || writeBusy.value) return;
  loading.value = true;
  message.value = "";
  detailController?.abort();
  detailController = null;
  selected.value = null;
  detailMessage.value = "";
  loadingDetail.value = false;
  const query = new URLSearchParams({
    limit: String(pageSize),
    offset: String(nextOffset),
  });
  if (filters.status) query.set("status", filters.status);
  if (filters.number) query.set("number", filters.number);
  try {
    const page = await auth.request<Page<OrderSummary>>(
      `${endpoint()}?${query}`,
      { signal: listController.signal },
    );
    if (listController.signal.aborted) return;
    orders.value = page.items;
    hasMore.value = page.has_more;
    offset.value = nextOffset;
  } catch (error) {
    orders.value = [];
    hasMore.value = false;
    if (!listController.signal.aborted)
      message.value = error instanceof Error ? error.message : "订单读取失败";
  } finally {
    loading.value = false;
  }
}
async function showDetail(id: string) {
  if (writeBusy.value) return;
  detailController?.abort();
  const controller = new AbortController();
  detailController = controller;
  selected.value = null;
  detailMessage.value = "";
  loadingDetail.value = true;
  try {
    const detail = await auth.request<OrderDetail>(`${endpoint()}/${id}`, {
      signal: controller.signal,
    });
    if (!controller.signal.aborted) selected.value = detail;
  } catch (error) {
    if (!controller.signal.aborted)
      detailMessage.value =
        error instanceof Error ? error.message : "详情读取失败";
  } finally {
    if (detailController === controller) loadingDetail.value = false;
  }
}
function cancelled(receipt: CancellationReceipt) {
  if (selected.value?.id === receipt.order_id) {
    selected.value = {
      ...selected.value,
      status: "cancelled",
      cancellation: receipt,
    };
  }
  orders.value = orders.value
    .map((order) =>
      order.id === receipt.order_id
        ? { ...order, status: "cancelled" as const }
        : order,
    )
    .filter((order) => !filters.status || order.status === filters.status);
  writeBusy.value = false;
  detailNotice.value = "取消结果已保存";
  void showDetail(receipt.order_id);
}
async function workflowChanged(receipt: WorkflowReceipt) {
  writeBusy.value = false;
  detailNotice.value = "办理结果已保存";
  orders.value = orders.value
    .map((row) =>
      row.id === receipt.order_id
        ? { ...row, status: receipt.order_status as OrderStatus }
        : row,
    )
    .filter((row) => !filters.status || row.status === filters.status);
  await showDetail(receipt.order_id);
}
onMounted(async () => {
  await load();
  const id = route?.query?.order_id;
  if (typeof id === "string") await showDetail(id);
});
onUnmounted(() => {
  listController.abort();
  detailController?.abort();
});
</script>
<template>
  <section class="status-page">
    <div class="page-heading">
      <div>
        <p class="eyebrow">{{ admin ? "ORDER WORKSPACE" : "YOUR ORDERS" }}</p>
        <h1>{{ admin ? "订单管理" : "我的订单" }}</h1>
        <p>
          {{
            admin
              ? "查看订单状态与物流记录，定位需要跟进的问题。"
              : "每一笔订单，都有清楚的来处与进度。"
          }}
        </p>
      </div>
      <button
        class="button button-outline"
        :disabled="loading || writeBusy"
        @click="load()"
      >
        刷新 ↻
      </button>
    </div>
    <p class="notice">
      演示环境 · 可体验付款、发货、收货与退货退款，不涉及真实资金或物流渠道。
    </p>
    <form class="order-filters panel" @submit.prevent="search">
      <label
        >订单状态<select v-model="status" :disabled="loading || writeBusy">
          <option value="">全部状态</option>
          <option
            v-for="(label, value) in orderStatuses"
            :key="value"
            :value="value"
          >
            {{ label }}
          </option>
        </select></label
      ><label class="order-number-filter"
        >订单号<input
          v-model="number"
          maxlength="40"
          placeholder="输入完整订单号"
          :disabled="loading || writeBusy" /></label
      ><button class="button button-dark" :disabled="loading || writeBusy">
        {{ loading ? "正在查询…" : "查询订单" }}
      </button>
    </form>
    <p v-if="message" role="alert" class="notice notice-warning">
      {{ message }}
    </p>
    <div class="panel table-panel" :aria-busy="loading">
      <div class="section-heading">
        <h2>订单列表</h2>
        <span>{{
          loading ? "正在读取…" : `第 ${offset / pageSize + 1} 页`
        }}</span>
      </div>
      <div class="table-scroll">
        <table class="accounts-table orders-table">
          <thead>
            <tr>
              <th>订单号</th>
              <th v-if="admin">用户</th>
              <th>状态</th>
              <th>金额</th>
              <th>创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="order in orders" :key="order.id">
              <td>
                {{ order.number
                }}<small v-if="order.is_demo" class="order-demo"
                  >演示订单</small
                >
              </td>
              <td v-if="admin">{{ order.customer_username }}</td>
              <td>
                <span
                  class="badge"
                  :class="
                    order.status === 'cancelled'
                      ? 'badge-neutral'
                      : 'badge-good'
                  "
                  >{{ orderStatuses[order.status] }}</span
                >
              </td>
              <td>{{ money(order.total_cents) }}</td>
              <td>{{ dateTime(order.created_at) }}</td>
              <td>
                <button
                  type="button"
                  class="text-button"
                  :disabled="loading || writeBusy"
                  @click="showDetail(order.id)"
                >
                  查看详情 ↗
                </button>
              </td>
            </tr>
            <tr v-if="!orders.length && !loading && !message">
              <td :colspan="admin ? 6 : 5">暂无符合条件的订单</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <button
          class="button button-outline"
          :disabled="loading || writeBusy || offset === 0"
          @click="load(offset - pageSize)"
        >
          上一页</button
        ><button
          class="button button-outline"
          :disabled="
            loading || writeBusy || !hasMore || offset + pageSize > 10000
          "
          @click="load(offset + pageSize)"
        >
          下一页
        </button>
      </div>
    </div>
    <p v-if="loadingDetail" class="notice" role="status">正在读取订单详情…</p>
    <p v-if="detailMessage" class="notice notice-warning" role="alert">
      {{ detailMessage }}
    </p>
    <p v-if="detailNotice" class="notice" role="status">{{ detailNotice }}</p>
    <section v-if="selected" class="order-detail panel" aria-label="订单详情">
      <div class="section-heading">
        <div>
          <p class="eyebrow">ORDER DETAILS</p>
          <h2>订单详情</h2>
        </div>
        <span class="badge badge-good">{{
          orderStatuses[selected.status]
        }}</span>
      </div>
      <p class="order-detail-number">{{ selected.number }}</p>
      <p class="form-description">
        创建于 {{ dateTime(selected.created_at)
        }}<span v-if="selected.is_demo"> · 演示订单</span>
      </p>
      <ul class="order-items">
        <li v-for="item in selected.items" :key="item.id">
          <div>
            <strong>{{ productText(item.product_name) }}</strong
            ><small
              >{{ item.sku }} · {{ money(item.unit_price_cents) }} ×
              {{ item.quantity }}</small
            >
          </div>
          <strong>{{ money(item.unit_price_cents * item.quantity) }}</strong>
        </li>
      </ul>
      <p class="order-total">
        订单合计 <strong>{{ money(selected.total_cents) }}</strong>
      </p>
      <OrderBusinessPanel
        :key="selected.id"
        :order="selected"
        :admin="admin"
        @cancelled="cancelled"
        @changed="workflowChanged"
        @busy="writeBusy = $event"
      />
      <div class="order-logistics">
        <h3>物流进度</h3>
        <p v-if="!selected.shipments.length">当前订单暂无物流记录。</p>
        <article v-for="shipment in selected.shipments" :key="shipment.id">
          <p>
            {{ shipment.carrier
            }}<span v-if="shipment.is_demo" class="badge badge-neutral"
              >演示运单</span
            >
          </p>
          <small>运单号 {{ shipment.tracking_number }}</small>
          <ol class="shipment-timeline">
            <li v-for="event in shipment.events" :key="event.id">
              <time>{{ dateTime(event.occurred_at) }}</time
              ><span>{{ event.description }}</span>
            </li>
          </ol>
        </article>
      </div>
    </section>
  </section>
</template>
