<script setup lang="ts">
import {
  computed,
  nextTick,
  onMounted,
  onUnmounted,
  reactive,
  ref,
  watch,
} from "vue";
import { auth } from "../auth/session";
import { money, productText, type Page, type Product } from "../api/orders";
import ProductActions from "../components/ProductActions.vue";
import {
  productFields,
  type Candidate,
  type CandidatePage,
  type ImportResult,
} from "../api/catalog";
import { ApiError } from "../auth/client";

const tab = ref<"candidates" | "products" | "trash">("candidates");
const candidates = ref<Candidate[]>([]),
  products = ref<Product[]>([]);
const categories = ref<string[]>([]),
  query = ref(""),
  category = ref("");
const submitted = { q: "", category: "" };
const loading = ref(false),
  saving = ref(false),
  message = ref(""),
  success = ref("");
const offset = ref(0),
  hasMore = ref(false),
  pageSize = 12;
const selected = ref<Candidate | null>(null);
const activeProduct = ref<string | null>(null);
const form = reactive({
  sku: "",
  name: "",
  description: "",
  price: "",
  stock: "0",
});
const manual = ref(false);
const entryOpened = computed(() => manual.value || selected.value !== null);
const entryDialog = ref<HTMLDialogElement | null>(null);
const pending = ref<{ path: string; body: string } | null>(null);
let manualRequestId = "";
let opener: HTMLElement | null = null;
let reader: AbortController | undefined;
const writer = new AbortController();

async function load(nextOffset = 0) {
  reader?.abort();
  const current = new AbortController();
  reader = current;
  loading.value = true;
  message.value = "";
  candidates.value = [];
  products.value = [];
  hasMore.value = false;
  try {
    const params = new URLSearchParams({
      limit: String(pageSize),
      offset: String(nextOffset),
    });
    if (tab.value === "candidates") {
      params.set("q", submitted.q);
      if (submitted.category) params.set("category", submitted.category);
      const page = await auth.request<CandidatePage>(
        `/api/admin/catalog/candidates?${params}`,
        { signal: current.signal },
      );
      if (current.signal.aborted) return;
      candidates.value = page.items;
      categories.value = page.categories;
      hasMore.value = page.has_more;
    } else {
      if (tab.value === "trash") params.set("deleted", "true");
      const page = await auth.request<Page<Product>>(
        `/api/admin/catalog/products?${params}`,
        { signal: current.signal },
      );
      if (current.signal.aborted) return;
      products.value = page.items;
      hasMore.value = page.has_more;
    }
    offset.value = nextOffset;
  } catch (error) {
    if (!current.signal.aborted)
      message.value =
        error instanceof Error ? error.message : "商品读取失败，请重试";
  } finally {
    if (reader === current) loading.value = false;
  }
}
function search() {
  submitted.q = query.value.trim();
  submitted.category = category.value;
  void load();
}
function switchTab(next: typeof tab.value) {
  if (saving.value || entryOpened.value || next === tab.value) return;
  tab.value = next;
  selected.value = null;
  void load();
}
function rememberOpener(event?: Event) {
  opener =
    event?.currentTarget instanceof HTMLElement ? event.currentTarget : null;
  message.value = "";
  success.value = "";
}
function choose(candidate: Candidate, event?: Event) {
  if (saving.value || entryOpened.value || candidate.imported_product_id)
    return;
  rememberOpener(event);
  selected.value = candidate;
  form.name = productText(candidate.name);
  form.description = `${candidate.description}\n${Object.entries(
    candidate.specifications,
  )
    .map(([key, value]) => `${key}：${value}`)
    .join("；")}`;
  form.price = (candidate.suggested_price_cents / 100).toFixed(2);
  form.stock = "0";
}
function newProduct(event: Event) {
  if (saving.value || entryOpened.value) return;
  rememberOpener(event);
  Object.assign(form, {
    sku: "",
    name: "",
    description: "",
    price: "",
    stock: "0",
  });
  manualRequestId = crypto.randomUUID();
  manual.value = true;
}
function closeEditor() {
  if (saving.value || pending.value) return;
  entryDialog.value?.close();
  selected.value = null;
  manual.value = false;
  void nextTick(() => {
    const target =
      opener && opener.isConnected && !opener.matches(":disabled")
        ? opener
        : document.getElementById("new-product-button");
    target?.focus({ preventScroll: true });
  });
}
watch(entryOpened, async (open) => {
  if (!open) return;
  await nextTick();
  if (entryDialog.value && !entryDialog.value.open)
    entryDialog.value.showModal();
});
async function importProduct() {
  if (saving.value || !entryOpened.value) return;
  if (!pending.value) {
    try {
      const fields = productFields(form);
      if (manual.value) {
        const sku = form.sku.trim().toUpperCase();
        if (!/^[A-Z0-9][A-Z0-9._-]{2,31}$/.test(sku))
          throw new Error("SKU 需为 3～32 位字母、数字、点、横线或下划线");
        pending.value = {
          path: "/api/admin/catalog/products",
          body: JSON.stringify({ ...fields, sku, request_id: manualRequestId }),
        };
      } else
        pending.value = {
          path: "/api/admin/catalog/imports",
          body: JSON.stringify({ candidate_id: selected.value!.id, ...fields }),
        };
    } catch (error) {
      message.value = error instanceof Error ? error.message : "请检查商品资料";
      return;
    }
  }
  saving.value = true;
  message.value = "";
  try {
    const result = await auth.request<ImportResult>(pending.value.path, {
      method: "POST",
      signal: writer.signal,
      body: pending.value.body,
    });
    if (writer.signal.aborted) return;
    const wasManual = manual.value;
    const visible = candidates.value.find(
      (item) => item.id === selected.value?.id,
    );
    if (visible) visible.imported_product_id = result.product.id;
    pending.value = null;
    saving.value = false;
    closeEditor();
    success.value = result.created
      ? `「${result.product.name}」已入库，用户商品目录可见。`
      : "该商品已入库，保留首次入库的售价和库存，未重复创建。";
    if (wasManual) {
      tab.value = "products";
      void load();
    }
  } catch (error) {
    if (writer.signal.aborted) return;
    message.value = error instanceof Error ? error.message : "入库结果未确认";
    if (error instanceof ApiError && error.status >= 400 && error.status < 500)
      pending.value = null;
    else message.value += "。可重试同一商品，不会重复入库或增加库存。";
  } finally {
    saving.value = false;
  }
}
onMounted(() => load());
onUnmounted(() => {
  reader?.abort();
  writer.abort();
});
</script>

<template>
  <section class="status-page catalog-page">
    <div class="page-heading">
      <div>
        <p class="eyebrow">CATALOG STUDIO</p>
        <h1>让商品，从这里上架。</h1>
        <p>搜索候选商品，核对资料，再建立自己的商品目录。</p>
      </div>
      <button
        id="new-product-button"
        class="button button-primary"
        :disabled="saving || entryOpened"
        @click="newProduct"
      >
        新增商品 ＋
      </button>
    </div>
    <div class="catalog-tabs" aria-label="商品管理视图">
      <button
        :class="{ active: tab === 'candidates' }"
        :disabled="saving"
        @click="switchTab('candidates')"
      >
        选品入库 <span>01</span>
      </button>
      <button
        :class="{ active: tab === 'products' }"
        :disabled="saving"
        @click="switchTab('products')"
      >
        已入库目录 <span>02</span>
      </button>
      <button
        :class="{ active: tab === 'trash' }"
        :disabled="saving"
        @click="switchTab('trash')"
      >
        回收站 <span>03</span>
      </button>
    </div>
    <p class="notice catalog-disclosure">
      演示环境 · 商品资料用于功能体验，销售价格与可用库存由管理员维护。
    </p>
    <p
      v-if="message && !entryOpened"
      class="notice notice-warning"
      role="alert"
    >
      {{ message }}
    </p>
    <p v-if="success" class="notice" role="status">{{ success }}</p>
    <form
      v-if="tab === 'candidates'"
      class="order-filters catalog-search"
      @submit.prevent="search"
    >
      <label class="order-number-filter"
        >搜索商品<input
          v-model="query"
          maxlength="80"
          placeholder="试试「显卡」「16GB」或「机械键盘」"
          :disabled="saving"
      /></label>
      <label
        >商品分类<select v-model="category" :disabled="saving">
          <option value="">全部分类</option>
          <option v-for="item in categories" :key="item">{{ item }}</option>
        </select></label
      >
      <button class="button button-primary" :disabled="loading || saving">
        搜索候选 ↗
      </button>
    </form>
    <div v-else class="section-heading">
      <p>
        {{
          tab === "trash"
            ? "回收站商品对用户不可见；恢复后默认下架，历史订单记录保留。"
            : "上架商品展示给用户，历史订单保留购买时的商品快照。"
        }}
      </p>
      <button
        class="button button-outline"
        :disabled="loading || saving"
        @click="load(offset)"
      >
        刷新 ↻
      </button>
    </div>
    <p v-if="loading" role="status">正在读取商品…</p>
    <div class="catalog-workspace" :aria-busy="loading">
      <div>
        <div v-if="tab === 'candidates'" class="catalog-cards">
          <article
            v-for="item in candidates"
            :key="item.id"
            class="catalog-card"
            :class="{ selected: selected?.id === item.id }"
          >
            <div
              class="catalog-art"
              :data-category="item.category"
              aria-hidden="true"
            >
              <span>{{ item.category }}</span>
              <div class="catalog-device">
                {{
                  item.category === "显卡"
                    ? "◉ ◉"
                    : item.category === "键盘"
                      ? "▦"
                      : item.category === "耳机"
                        ? "◡"
                        : "▣"
                }}
              </div>
              <small>{{ item.id }}</small>
            </div>
            <div class="catalog-card-body">
              <div class="section-heading">
                <small>{{ item.category }}</small>
              </div>
              <h2>{{ item.name }}</h2>
              <div class="catalog-specs">
                <span v-for="(value, key) in item.specifications" :key="key"
                  >{{ key }} {{ value }}</span
                >
              </div>
              <div class="catalog-card-bottom">
                <div>
                  <small>参考价</small
                  ><strong>{{ money(item.suggested_price_cents) }}</strong>
                </div>
                <button
                  class="button button-outline"
                  :disabled="saving || !!item.imported_product_id"
                  @click="choose(item, $event)"
                >
                  {{ item.imported_product_id ? "已入库 ✓" : "选择入库 +" }}
                </button>
              </div>
            </div>
          </article>
        </div>
        <div v-else class="panel catalog-saved">
          <article v-for="product in products" :key="product.id">
            <div>
              <small
                >{{ product.sku }} ·
                {{
                  product.deleted_at
                    ? "回收站"
                    : product.is_active
                      ? "已上架"
                      : "已下架"
                }}</small
              >
              <h2>{{ productText(product.name) }}</h2>
              <p>{{ productText(product.description) }}</p>
              <ProductActions
                :key="`${product.id}:${product.version}`"
                :product="product"
                :blocked="saving && activeProduct !== product.id"
                @changed="
                  success = '商品已更新';
                  load(offset);
                "
                @busy="
                  saving = $event;
                  activeProduct = $event ? product.id : null;
                "
              />
            </div>
            <div>
              <strong>{{ money(product.price_cents) }}</strong
              ><small>库存 {{ product.stock }}</small>
            </div>
          </article>
        </div>
        <div
          v-if="
            !loading &&
            !message &&
            !(tab === 'candidates' ? candidates.length : products.length)
          "
          class="panel catalog-empty"
        >
          <h2>
            {{
              tab === "candidates"
                ? "没有找到匹配的候选商品"
                : tab === "trash"
                  ? "回收站为空"
                  : "目录还没有商品"
            }}
          </h2>
          <p>
            {{
              tab === "candidates"
                ? "可以更换关键词或选择全部分类。"
                : tab === "trash"
                  ? "删除的商品会保存在这里，可以随时恢复。"
                  : "切换到选品入库，选择第一件商品。"
            }}
          </p>
        </div>
        <div class="pagination">
          <button
            class="button button-outline"
            :disabled="loading || saving || offset === 0"
            @click="load(offset - pageSize)"
          >
            上一页</button
          ><span>第 {{ offset / pageSize + 1 }} 页</span
          ><button
            class="button button-outline"
            :disabled="
              loading || saving || !hasMore || offset + pageSize > 10000
            "
            @click="load(offset + pageSize)"
          >
            下一页
          </button>
        </div>
      </div>
      <dialog
        v-if="entryOpened"
        ref="entryDialog"
        class="panel catalog-editor"
        aria-labelledby="import-heading"
        @cancel.prevent="closeEditor"
      >
        <div class="section-heading">
          <p class="eyebrow">
            {{ manual ? "NEW PRODUCT" : "READY TO IMPORT" }}
          </p>
          <button
            class="text-button"
            :disabled="saving || !!pending"
            @click="closeEditor"
          >
            关闭 ×
          </button>
        </div>
        <h2 id="import-heading">{{ manual ? "新增商品" : "核对，再入库" }}</h2>
        <p class="catalog-editor-help">
          {{
            manual
              ? "填写自己的商品资料，确认后加入商品目录。"
              : `${selected!.id} · ${selected!.category}`
          }}
        </p>
        <p v-if="message" class="notice notice-warning" role="alert">
          {{ message }}
        </p>
        <form @submit.prevent="importProduct">
          <fieldset :disabled="saving || !!pending">
            <label v-if="manual"
              >商品 SKU<input
                v-model="form.sku"
                required
                maxlength="32"
                placeholder="例如 OWN-MONITOR-001"
                autofocus
              /><small
                >3～32
                位字母、数字、点、横线或下划线；不可与现有商品重复。</small
              ></label
            >
            <label
              >商品名称<input
                v-model="form.name"
                required
                maxlength="120"
                :autofocus="!manual"
            /></label>
            <label
              >商品说明<textarea
                v-model="form.description"
                required
                maxlength="1000"
                rows="4"
              />
            </label>
            <div class="catalog-editor-numbers">
              <label
                >销售价格（元）<input
                  v-model="form.price"
                  required
                  inputmode="decimal"
                  placeholder="0.00" /></label
              ><label
                >初始库存（件）<input
                  v-model="form.stock"
                  required
                  inputmode="numeric"
              /></label>
            </div>
          </fieldset>
          <p class="catalog-editor-help">
            确认后展示在用户商品目录。请求重试不会重复创建商品或累加库存。
          </p>
          <button
            class="button button-primary"
            :disabled="saving"
            type="submit"
          >
            {{
              saving ? "正在入库…" : pending ? "重试原入库请求" : "确认入库 ↗"
            }}
          </button>
        </form>
      </dialog>
    </div>
  </section>
</template>

<style scoped>
.catalog-tabs {
  display: flex;
  gap: 32px;
  border-bottom: 1px solid var(--line);
  margin-bottom: 20px;
}
.catalog-tabs button {
  border: 0;
  border-bottom: 2px solid transparent;
  padding: 12px 0;
  background: none;
  color: var(--muted);
  font: inherit;
}
.catalog-tabs button.active {
  color: var(--ink);
  border-color: var(--accent);
}
.catalog-tabs span {
  margin-left: 12px;
  font-size: 10px;
  color: var(--muted);
}
.catalog-disclosure {
  font-size: 12px;
}
.catalog-search {
  margin: 28px 0;
}
.catalog-workspace {
  display: grid;
  gap: 24px;
  align-items: start;
}
.catalog-cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 20px;
}
.catalog-card {
  border: 1px solid var(--line);
  border-radius: 12px;
  overflow: hidden;
  background: white;
}
.catalog-card.selected {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}
.catalog-art {
  height: 150px;
  padding: 16px 20px;
  position: relative;
  display: grid;
  place-items: center;
  background: #e9eef0;
  color: #58717e;
}
.catalog-art[data-category="显卡"] {
  background: #e9eeea;
  color: #526c61;
}
.catalog-art[data-category="键盘"] {
  background: #f3ece5;
  color: #9d7759;
}
.catalog-art[data-category="耳机"] {
  background: #eeeaf2;
  color: #867496;
}
.catalog-art > span {
  position: absolute;
  top: 12px;
  left: 16px;
  font-size: 10px;
  letter-spacing: 2px;
}
.catalog-art > small {
  position: absolute;
  bottom: 10px;
  right: 15px;
  font-size: 9px;
  letter-spacing: 2px;
  opacity: 0.7;
}
.catalog-device {
  border: 2px solid currentColor;
  border-radius: 12px;
  padding: 0 18px;
  font-size: 42px;
  letter-spacing: 3px;
  box-shadow: 6px 6px 0 #00000008;
}
.catalog-card-body {
  padding: 20px;
}
.catalog-card h2 {
  font-size: 15px;
  margin: 14px 0;
  line-height: 1.6;
}
.catalog-card .section-heading {
  margin: 0;
}
.catalog-card small {
  font-size: 10px;
  color: var(--muted);
}
.catalog-specs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-height: 25px;
}
.catalog-specs span {
  font-size: 10px;
  padding: 3px 7px;
  background: #f5f6f7;
  border-radius: 4px;
  color: #667982;
}
.catalog-card-bottom {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  border-top: 1px solid var(--line);
  padding-top: 18px;
  margin-top: 20px;
}
.catalog-card-bottom strong {
  display: block;
  font-size: 20px;
  font-weight: 500;
}
.catalog-card-bottom .button {
  font-size: 11px;
  padding: 8px 12px;
}
.catalog-editor {
  position: fixed;
  inset: 0;
  width: min(560px, calc(100vw - 32px));
  max-height: calc(100dvh - 48px);
  overflow-y: auto;
  overscroll-behavior: contain;
  margin: auto;
  padding: 28px;
  border: 1px solid var(--line);
  color: var(--ink);
  box-shadow: 0 24px 80px #172d3d33;
}
.catalog-editor::backdrop {
  background: #172d3d66;
}
.catalog-editor h2 {
  font-size: 22px;
  margin: 12px 0 6px;
  font-weight: 500;
}
.catalog-editor label small {
  color: var(--muted);
  font-size: 10px;
  font-weight: 400;
}
.catalog-editor-help {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.9;
}
.catalog-editor fieldset {
  padding: 0;
  border: 0;
  min-width: 0;
}
.catalog-editor label {
  display: grid;
  gap: 8px;
  margin-top: 18px;
  font-size: 12px;
}
.catalog-editor input,
.catalog-editor textarea {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--line);
  padding: 10px;
  border-radius: 6px;
  background: white;
  color: var(--ink);
  font: inherit;
}
.catalog-editor textarea {
  resize: vertical;
}
.catalog-editor-numbers {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.catalog-editor .button-primary {
  width: 100%;
  margin-top: 10px;
  justify-content: center;
}
.catalog-saved article {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  padding: 20px 0;
  border-bottom: 1px solid var(--line);
}
.catalog-saved article:last-child {
  border: 0;
}
.catalog-saved h2 {
  font-size: 16px;
  margin: 8px 0;
}
.catalog-saved p {
  font-size: 12px;
  color: var(--muted);
  max-width: 620px;
  white-space: pre-line;
}
.catalog-saved small {
  display: block;
  font-size: 11px;
  color: var(--muted);
}
.catalog-saved strong {
  white-space: nowrap;
}
.catalog-empty {
  padding: 48px;
  text-align: center;
}
.catalog-empty h2 {
  font-size: 18px;
  font-weight: 500;
}
.catalog-empty p {
  font-size: 13px;
  color: var(--muted);
}
@media (max-width: 1300px) {
  .catalog-cards {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@media (max-width: 800px) {
  .catalog-cards {
    grid-template-columns: 1fr;
  }
  .catalog-saved article {
    flex-wrap: wrap;
  }
}
</style>
