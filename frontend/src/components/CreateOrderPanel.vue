<script setup lang="ts">
import { computed, onUnmounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { auth } from "../auth/session";
import { ApiError } from "../auth/client";
import {
  money,
  productText,
  type Product,
  type OrderDetail,
} from "../api/orders";

const props = defineProps<{ product: Product }>();
const emit = defineEmits<{
  completed: [];
  close: [];
  busy: [value: boolean];
}>();
const quantity = ref(1),
  acknowledged = ref(false),
  saving = ref(false),
  message = ref("");
const order = ref<OrderDetail | null>(null);
const pending = ref<string | null>(null);
const total = computed(() => props.product.price_cents * quantity.value);
const controller = new AbortController();
async function submit() {
  if (saving.value || order.value || !acknowledged.value) return;
  if (!pending.value) {
    if (
      !Number.isInteger(quantity.value) ||
      quantity.value < 1 ||
      quantity.value > Math.min(99, props.product.stock)
    ) {
      message.value = "请填写库存范围内的整数数量，最多 99 件";
      return;
    }
    pending.value = JSON.stringify({
      request_id: crypto.randomUUID(),
      product_id: props.product.id,
      quantity: quantity.value,
      expected_price_cents: props.product.price_cents,
    });
  }
  saving.value = true;
  emit("busy", true);
  message.value = "";
  try {
    const result = await auth.request<{ order: OrderDetail; created: boolean }>(
      "/api/orders",
      { method: "POST", body: pending.value, signal: controller.signal },
    );
    if (!controller.signal.aborted) {
      order.value = result.order;
      pending.value = null;
      emit("completed");
    }
  } catch (error) {
    if (!controller.signal.aborted) {
      message.value = error instanceof Error ? error.message : "下单结果未确认";
      if (
        error instanceof ApiError &&
        error.status >= 400 &&
        error.status < 500
      ) {
        pending.value = null;
        acknowledged.value = false;
        message.value += "。请关闭后刷新商品目录。";
      } else message.value += "。请手动重试同一请求，不会重复下单。";
    }
  } finally {
    saving.value = false;
    emit("busy", !!pending.value);
  }
}
onUnmounted(() => {
  controller.abort();
  emit("busy", false);
});
</script>
<template>
  <section class="panel checkout-panel" aria-labelledby="checkout-heading">
    <div class="section-heading">
      <h2 id="checkout-heading">{{ order ? "订单已创建" : "确认订单" }}</h2>
      <button
        class="text-button"
        :disabled="saving || !!pending"
        @click="emit('close')"
      >
        收起 ×
      </button>
    </div>
    <template v-if="order"
      ><p>{{ order.number }}</p>
      <p>
        待付款 ·
        {{
          money(order.total_cents)
        }}。库存已预占，可在订单详情中取消并释放库存。
      </p>
      <RouterLink class="button button-outline" to="/orders"
        >查看我的订单 ↗</RouterLink
      ></template
    >
    <form v-else @submit.prevent="submit">
      <p>
        {{ productText(product.name) }} · {{ money(product.price_cents) }}/件
      </p>
      <label class="checkout-quantity"
        >数量<input
          v-model.number="quantity"
          type="number"
          min="1"
          :max="Math.min(99, product.stock)"
          :disabled="saving || !!pending"
          required
      /></label>
      <p>
        合计 <strong>{{ money(total) }}</strong>
      </p>
      <label class="checkout-confirm"
        ><input
          v-model="acknowledged"
          type="checkbox"
          :disabled="saving || !!pending"
        />已核对商品、数量和金额，确认创建订单</label
      >
      <p class="checkout-note">演示环境，不发起支付。订单创建后预占库存。</p>
      <button class="button button-primary" :disabled="saving || !acknowledged">
        {{ saving ? "正在创建…" : pending ? "重试原下单请求" : "确认下单" }}
      </button>
    </form>
    <p v-if="message" class="notice notice-warning" role="alert">
      {{ message }}
    </p>
  </section>
</template>
<style scoped>
.checkout-panel {
  margin: 24px 0;
  border: 1px solid #cad9d0;
}
.checkout-panel h2 {
  font-size: 20px;
}
.checkout-panel p {
  overflow-wrap: anywhere;
  font-size: 13px;
}
.checkout-quantity {
  display: flex;
  align-items: center;
  gap: 16px;
  font-size: 12px;
}
.checkout-quantity input {
  width: 90px;
  padding: 8px;
  border: 1px solid var(--line);
  border-radius: 6px;
}
.checkout-confirm {
  display: flex;
  gap: 8px;
  font-size: 12px;
  align-items: center;
}
.checkout-note {
  color: var(--muted);
}
</style>
