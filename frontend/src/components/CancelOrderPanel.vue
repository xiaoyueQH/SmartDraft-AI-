<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { auth } from "../auth/session";
import { ApiError } from "../auth/client";
import {
  dateTime,
  money,
  orderStatuses,
  type CancellationPreview,
  type CancellationReceipt,
  type OrderDetail,
} from "../api/orders";

const props = defineProps<{
  order: OrderDetail;
  admin?: boolean;
  blocked?: boolean;
}>();
const emit = defineEmits<{
  cancelled: [receipt: CancellationReceipt];
  busy: [value: boolean];
}>();
const reason = ref(""),
  message = ref(""),
  acknowledged = ref(false);
const preview = ref<CancellationPreview | null>(null),
  receipt = ref<CancellationReceipt | null>(null);
const preparing = ref(false),
  submitting = ref(false),
  uncertain = ref(false);
const controller = new AbortController();
watch(
  () =>
    preparing.value || submitting.value || uncertain.value || !!preview.value,
  (value) => emit("busy", value),
);
const completed = computed(() => receipt.value || props.order.cancellation);
const eligible = computed(
  () =>
    ["pending_payment", "paid"].includes(props.order.status) &&
    (props.order.status !== "paid" || props.order.is_demo),
);
const endpoint = () =>
  `${props.admin ? "/api/admin/orders" : "/api/orders"}/${props.order.id}`;

async function prepare() {
  if (props.blocked || preparing.value || submitting.value || uncertain.value)
    return;
  preparing.value = true;
  message.value = "";
  acknowledged.value = false;
  try {
    const result = await auth.request<CancellationPreview>(
      `${endpoint()}/cancellation-preview`,
      {
        method: "POST",
        body: JSON.stringify({ reason: reason.value }),
        signal: controller.signal,
      },
    );
    if (!controller.signal.aborted) preview.value = result;
  } catch (error) {
    if (!controller.signal.aborted)
      message.value =
        error instanceof Error ? error.message : "暂时无法检查取消条件，请重试";
  } finally {
    preparing.value = false;
  }
}

async function confirm() {
  if (
    props.blocked ||
    !preview.value ||
    !acknowledged.value ||
    submitting.value
  )
    return;
  submitting.value = true;
  message.value = "";
  const confirmation = preview.value;
  try {
    const result = await auth.request<CancellationReceipt>(
      `${endpoint()}/cancel`,
      {
        method: "POST",
        body: JSON.stringify({
          confirmation_id: confirmation.confirmation_id,
          confirmed: true,
        }),
        signal: controller.signal,
      },
    );
    if (controller.signal.aborted) return;
    receipt.value = result;
    uncertain.value = false;
    preview.value = null;
    emit("cancelled", result);
  } catch (error) {
    if (controller.signal.aborted) return;
    if (
      error instanceof ApiError &&
      error.status >= 400 &&
      error.status < 500
    ) {
      preview.value = null;
      acknowledged.value = false;
      uncertain.value = false;
      message.value = error.message;
    } else {
      uncertain.value = true;
      message.value =
        "取消结果尚未确认。请重试原操作以查询或完成同一笔取消，也可以刷新订单核对；不要重复创建新请求。";
    }
  } finally {
    submitting.value = false;
  }
}
onUnmounted(() => controller.abort());
</script>

<template>
  <section class="cancellation-panel" aria-label="取消订单">
    <template v-if="completed">
      <div class="section-heading">
        <h3>取消已完成</h3>
        <span class="badge badge-good">已保存</span>
      </div>
      <p>
        操作人 {{ completed.actor_username }} ·
        {{ dateTime(completed.completed_at) }}
      </p>
      <p>取消原因：{{ completed.reason }}</p>
      <p>已释放 {{ completed.released_units }} 件预占库存。</p>
      <p v-if="completed.refund_status === 'simulated'" class="notice">
        已保存演示退款记录
        {{ money(completed.refund_cents) }}，没有向支付渠道发起真实退款。
      </p>
      <p v-else>未付款订单，无需退款。</p>
      <small class="cancellation-reference"
        >操作编号 {{ completed.confirmation_id }}</small
      >
    </template>
    <template v-else-if="eligible">
      <h3>取消订单</h3>
      <p>
        先检查取消条件，核对结果后再确认。此操作会结束订单并释放其预占库存。
      </p>
      <p v-if="message" class="notice notice-warning" role="alert">
        {{ message }}
      </p>
      <form v-if="!preview" @submit.prevent="prepare">
        <label
          >取消原因<textarea
            v-model="reason"
            required
            maxlength="300"
            rows="2"
            :disabled="preparing || blocked"
            placeholder="请简要说明取消原因"
          />
        </label>
        <button
          type="submit"
          class="button button-outline"
          :disabled="preparing || blocked || !reason.trim()"
        >
          {{ preparing ? "正在检查…" : "检查取消条件" }}
        </button>
      </form>
      <section v-else class="cancel-confirmation" aria-label="取消确认">
        <h4>请核对取消信息</h4>
        <p>
          {{ preview.number }} · {{ orderStatuses[preview.from_status] }} ·
          {{ money(preview.total_cents) }}
        </p>
        <p>取消原因：{{ preview.reason }}</p>
        <ul>
          <li v-for="(item, index) in preview.release_items" :key="index">
            {{ item.product_name }}：释放 {{ item.quantity }} 件预占库存
          </li>
        </ul>
        <p v-if="preview.refund_status === 'simulated'" class="notice">
          将保存演示退款记录
          {{ money(preview.refund_cents) }}，不会发生真实资金退回。
        </p>
        <p v-else>此订单未付款，无需退款。</p>
        <small
          >确认有效期至
          {{ dateTime(preview.expires_at) }}；执行前会再次检查订单状态。</small
        >
        <label class="cancel-acknowledge"
          ><input
            v-model="acknowledged"
            type="checkbox"
            :disabled="submitting || uncertain || blocked"
          />我已核对以上信息，确认取消这笔订单。</label
        >
        <div class="cancellation-actions">
          <button
            type="button"
            class="button button-dark"
            :disabled="submitting || blocked || !acknowledged"
            @click="confirm"
          >
            {{
              submitting
                ? "正在处理…"
                : uncertain
                  ? "查询 / 重试原操作"
                  : "确认取消订单"
            }}</button
          ><button
            v-if="!uncertain"
            type="button"
            class="button button-outline"
            :disabled="submitting || blocked"
            @click="
              preview = null;
              acknowledged = false;
            "
          >
            返回修改
          </button>
        </div>
      </section>
    </template>
    <p v-else-if="order.status !== 'cancelled'">
      {{
        order.status === "paid"
          ? "真实已付款订单需通过退款流程处理，当前尚未接入。"
          : "当前订单已发货或已签收，不能直接取消，需通过售后流程处理。"
      }}
    </p>
    <p v-else>该订单已取消，暂无本次系统的取消操作记录。</p>
  </section>
</template>
