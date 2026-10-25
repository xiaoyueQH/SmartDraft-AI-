<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from "vue";
import { auth } from "../auth/session";
import { ApiError } from "../auth/client";
import {
  dateTime,
  money,
  type CancellationReceipt,
  type OrderDetail,
} from "../api/orders";
import {
  returnStatuses,
  type WorkflowAction,
  type WorkflowReceipt,
} from "../api/workflow";
import CancelOrderPanel from "./CancelOrderPanel.vue";

const props = defineProps<{ order: OrderDetail; admin?: boolean }>();
const emit = defineEmits<{
  changed: [receipt: WorkflowReceipt];
  cancelled: [receipt: CancellationReceipt];
  busy: [value: boolean];
}>();
const active = ref<WorkflowAction | "">("");
const working = ref(false),
  cancelBusy = ref(false),
  message = ref(""),
  acknowledged = ref(false);
const form = reactive({
  reason: "",
  carrier: "",
  tracking_number: "",
  decision: "approve",
});
const pending = ref<{ url: string; body: string } | null>(null);
const controller = new AbortController();
const labels: Record<WorkflowAction, string> = {
  pay: "确认演示付款",
  ship: "登记发货",
  receive: "确认收货",
  return_request: "申请退货退款",
  return_review: "审核退货申请",
  return_ship: "登记退货运单",
  return_receive: "验收退货并补回库存",
  return_refund: "登记演示退款并关闭工单",
};
const options = computed<WorkflowAction[]>(() => {
  if (!props.order.is_demo) return [];
  const result: WorkflowAction[] = [],
    c = props.order.after_sale;
  if (!props.admin && props.order.status === "pending_payment")
    result.push("pay");
  if (props.admin && props.order.status === "paid") result.push("ship");
  if (!props.admin && props.order.status === "shipped") result.push("receive");
  if (!props.admin && props.order.status === "delivered" && !c)
    result.push("return_request");
  if (props.admin && c?.status === "requested") result.push("return_review");
  if (!props.admin && c?.status === "approved") result.push("return_ship");
  if (props.admin && c?.status === "returning") result.push("return_receive");
  if (props.admin && c?.status === "received") result.push("return_refund");
  return result;
});
watch(
  () => !!active.value || working.value || cancelBusy.value || !!pending.value,
  (value) => emit("busy", value),
);
function begin(action: WorkflowAction) {
  if (active.value || working.value || cancelBusy.value) return;
  active.value = action;
  acknowledged.value = false;
  message.value = "";
  Object.assign(form, {
    reason: "",
    carrier: "",
    tracking_number: "",
    decision: "approve",
  });
}
function close() {
  if (!working.value && !pending.value) active.value = "";
}
async function send() {
  if (!active.value || working.value || !acknowledged.value) return;
  if (!pending.value) {
    const action = active.value;
    const payload: Record<string, unknown> = {
      operation_id: crypto.randomUUID(),
      confirmed: true,
    };
    if (["ship", "return_ship"].includes(action)) {
      if (
        form.carrier.trim().length < 2 ||
        !/^[A-Za-z0-9-]{3,64}$/.test(form.tracking_number.trim())
      ) {
        message.value = "请填写物流公司和有效运单号";
        return;
      }
      Object.assign(payload, {
        carrier: form.carrier.trim(),
        tracking_number: form.tracking_number.trim(),
      });
    }
    if (["return_request", "return_review"].includes(action)) {
      if (form.reason.trim().length < 3) {
        message.value = "请填写至少 3 个字的原因或审核意见";
        return;
      }
      payload.reason = form.reason.trim();
    }
    if (action === "return_review") payload.decision = form.decision;
    const paths: Record<WorkflowAction, string> = {
      pay: "pay",
      ship: "ship",
      receive: "receive",
      return_request: "returns",
      return_review: "returns/review",
      return_ship: "returns/ship",
      return_receive: "returns/receive",
      return_refund: "returns/refund",
    };
    pending.value = {
      url: `${props.admin ? "/api/admin/orders" : "/api/orders"}/${props.order.id}/${paths[action]}`,
      body: JSON.stringify(payload),
    };
  }
  working.value = true;
  message.value = "";
  try {
    const result = await auth.request<WorkflowReceipt>(pending.value.url, {
      method: "POST",
      body: pending.value.body,
      signal: controller.signal,
    });
    if (controller.signal.aborted) return;
    pending.value = null;
    active.value = "";
    emit("busy", false);
    emit("changed", result);
  } catch (error) {
    if (controller.signal.aborted) return;
    message.value = error instanceof Error ? error.message : "结果未确认";
    if (
      error instanceof ApiError &&
      error.status >= 400 &&
      error.status < 500
    ) {
      pending.value = null;
      acknowledged.value = false;
      message.value += "。请返回并刷新订单后核对。";
    } else message.value += "。请重试原操作，不要重新创建请求。";
  } finally {
    working.value = false;
  }
}
onUnmounted(() => controller.abort());
</script>
<template>
  <section class="business-panel" aria-label="订单办理">
    <div v-if="order.payment || order.refund" class="business-ledger">
      <p v-if="order.payment">
        演示付款已记录 · {{ money(order.payment.amount_cents) }} ·
        {{ dateTime(order.payment.paid_at)
        }}<small>付款编号 {{ order.payment.id }}</small>
      </p>
      <p v-if="order.refund">
        演示退款已记录 · {{ money(order.refund.amount_cents) }} ·
        {{ dateTime(order.refund.refunded_at)
        }}<small>退款编号 {{ order.refund.id }}</small>
      </p>
    </div>
    <article v-if="order.after_sale" class="business-case">
      <div class="section-heading">
        <h3>退货退款工单</h3>
        <span class="badge badge-neutral">{{
          returnStatuses[order.after_sale.status]
        }}</span>
      </div>
      <p>申请原因：{{ order.after_sale.reason }}</p>
      <p v-if="order.after_sale.review_note">
        审核意见：{{ order.after_sale.review_note }}
      </p>
      <p>申请金额 {{ money(order.after_sale.refund_cents) }} · 整单退货</p>
      <p v-if="order.after_sale.tracking_number">
        退货运单：{{ order.after_sale.carrier }} /
        {{ order.after_sale.tracking_number }}
      </p>
      <p v-if="order.after_sale.restocked_at">
        商家已验收并补回库存 · {{ dateTime(order.after_sale.restocked_at) }}
      </p>
      <small>工单编号 {{ order.after_sale.id }}</small>
    </article>
    <div class="cancellation-actions">
      <button
        v-for="action in options"
        :key="action"
        class="button button-outline"
        :disabled="!!active || cancelBusy || working"
        @click="begin(action)"
      >
        {{ labels[action] }}
      </button>
    </div>
    <form v-if="active" class="business-confirm" @submit.prevent="send">
      <h3>{{ labels[active] }}</h3>
      <p>订单 {{ order.number }} · 金额 {{ money(order.total_cents) }}</p>
      <p v-if="active === 'pay' || active === 'return_refund'">
        此操作仅记录演示流水，不发生真实扣款或资金退回。
      </p>
      <p v-if="active === 'ship'">确认后将登记出库，订单不能再直接取消。</p>
      <p v-if="active === 'return_request'">
        申请退回此订单全部商品；每笔订单仅受理一次退货退款工单。
      </p>
      <p v-if="active === 'return_receive'">
        请确认已收到全部退回商品且验收通过，系统将补回对应库存。
      </p>
      <fieldset :disabled="working || !!pending">
        <label v-if="active === 'return_review'"
          >审核结果<select v-model="form.decision">
            <option value="approve">同意退货</option>
            <option value="reject">驳回申请</option>
          </select></label
        >
        <label v-if="active === 'return_request' || active === 'return_review'"
          >{{ active === "return_request" ? "申请原因" : "审核意见"
          }}<textarea
            v-model="form.reason"
            minlength="3"
            maxlength="300"
            rows="3"
            required
          />
        </label>
        <template v-if="active === 'ship' || active === 'return_ship'"
          ><label
            >物流公司<input
              v-model="form.carrier"
              required
              minlength="2"
              maxlength="64" /></label
          ><label
            >运单号<input
              v-model="form.tracking_number"
              required
              maxlength="64" /></label
        ></template>
        <label class="business-ack"
          ><input
            v-model="acknowledged"
            type="checkbox"
          />我已核对以上信息，确认执行此操作</label
        >
      </fieldset>
      <div class="cancellation-actions">
        <button
          class="button button-primary"
          :disabled="working || !acknowledged"
        >
          {{
            working ? "正在办理…" : pending ? "重试原操作" : "确认办理"
          }}</button
        ><button
          type="button"
          class="button button-outline"
          :disabled="working || !!pending"
          @click="close"
        >
          返回
        </button>
      </div>
      <p v-if="message" class="notice notice-warning" role="alert">
        {{ message }}
      </p>
    </form>
    <CancelOrderPanel
      :order="order"
      :admin="admin"
      :blocked="!!active || working || !!pending"
      @busy="cancelBusy = $event"
      @cancelled="emit('cancelled', $event)"
    />
    <div v-if="order.events?.length" class="business-events">
      <h3>订单时间线</h3>
      <ol class="shipment-timeline">
        <li v-for="event in order.events" :key="event.id">
          <time
            >{{ dateTime(event.occurred_at) }} ·
            {{ event.actor_username }}</time
          ><span>{{ event.description }}</span>
        </li>
      </ol>
    </div>
  </section>
</template>
<style scoped>
.business-panel {
  margin-top: 24px;
}
.business-ledger,
.business-case,
.business-confirm {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 20px;
  margin: 18px 0;
  background: #fafbf9;
}
.business-panel h3 {
  font-size: 16px;
}
.business-panel p,
.business-panel small {
  font-size: 12px;
  overflow-wrap: anywhere;
}
.business-panel small {
  display: block;
  color: var(--muted);
}
.business-confirm fieldset {
  border: 0;
  padding: 0;
  min-width: 0;
}
.business-confirm label {
  display: grid;
  gap: 7px;
  font-size: 12px;
  margin: 14px 0;
}
.business-confirm input:not([type="checkbox"]),
textarea,
select {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 7px;
  padding: 10px;
  font: inherit;
  background: white;
}
.business-confirm .business-ack {
  display: flex;
  gap: 8px;
  align-items: center;
}
.business-events {
  border-top: 1px solid var(--line);
  padding-top: 18px;
  margin-top: 20px;
}
</style>
