<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from "vue";
import { auth } from "../auth/session";
import { money, productText, type Page, type Product } from "../api/orders";
import CreateOrderPanel from "../components/CreateOrderPanel.vue";
const selected = ref<Product | null>(null);
const checkoutBusy = ref(false);
async function choose(product: Product) {
  if (checkoutBusy.value) return;
  selected.value = product;
  await nextTick();
  document
    .getElementById("checkout-heading")
    ?.scrollIntoView({ behavior: "smooth", block: "center" });
}
const products = ref<Product[]>([]),
  loading = ref(false),
  message = ref("");
const offset = ref(0),
  hasMore = ref(false),
  pageSize = 20;
const controller = new AbortController();
async function load(nextOffset = offset.value) {
  if (loading.value) return;
  loading.value = true;
  message.value = "";
  try {
    const page = await auth.request<Page<Product>>(
      `/api/products?limit=${pageSize}&offset=${nextOffset}`,
      { signal: controller.signal },
    );
    if (controller.signal.aborted) return;
    products.value = page.items;
    hasMore.value = page.has_more;
    offset.value = nextOffset;
  } catch (error) {
    if (!controller.signal.aborted)
      message.value = error instanceof Error ? error.message : "商品读取失败";
  } finally {
    loading.value = false;
  }
}
onMounted(() => load());
onUnmounted(() => controller.abort());
</script>
<template>
  <section class="status-page">
    <div class="page-heading">
      <div>
        <p class="eyebrow">PRODUCT COLLECTION</p>
        <h1>商品目录</h1>
        <p>先了解商品，再跟进属于你的订单。</p>
      </div>
      <button
        class="button button-outline"
        :disabled="loading || checkoutBusy"
        @click="load()"
      >
        刷新 ↻
      </button>
    </div>
    <p class="notice">
      演示环境 · 可体验下单、履约与退货退款，不涉及真实资金或物流渠道。
    </p>
    <p v-if="message" class="notice notice-warning" role="alert">
      {{ message }}
    </p>
    <p v-if="loading" role="status">正在读取商品…</p>
    <div class="product-grid" :aria-busy="loading">
      <article
        v-for="(product, index) in products"
        :key="product.id"
        class="panel product-card"
      >
        <div class="product-art" aria-hidden="true">
          {{ ["◒", "◉", "◈"][index % 3] }}
        </div>
        <div class="section-heading">
          <small>{{ product.sku }}</small>
        </div>
        <h2>{{ productText(product.name) }}</h2>
        <p>{{ productText(product.description) }}</p>
        <div class="product-price">
          <strong>{{ money(product.price_cents) }}</strong
          ><span>库存 {{ product.stock }}</span>
        </div>
        <button
          class="button button-outline product-order-button"
          :disabled="!product.stock || loading || checkoutBusy"
          @click="choose(product)"
        >
          {{ product.stock ? "创建订单 ↗" : "暂时缺货" }}
        </button>
      </article>
    </div>
    <CreateOrderPanel
      v-if="selected"
      :key="selected.id"
      :product="selected"
      @close="selected = null"
      @completed="load()"
      @busy="checkoutBusy = $event"
    />
    <p v-if="!loading && !products.length && !message" class="notice">
      暂无商品
    </p>
    <div class="pagination">
      <button
        class="button button-outline"
        :disabled="loading || checkoutBusy || offset === 0"
        @click="load(offset - pageSize)"
      >
        上一页</button
      ><span>第 {{ offset / pageSize + 1 }} 页</span
      ><button
        class="button button-outline"
        :disabled="
          loading || checkoutBusy || !hasMore || offset + pageSize > 10000
        "
        @click="load(offset + pageSize)"
      >
        下一页
      </button>
    </div>
  </section>
</template>

<style scoped>
.product-order-button {
  width: 100%;
  justify-content: center;
  margin-top: 18px;
}
</style>
