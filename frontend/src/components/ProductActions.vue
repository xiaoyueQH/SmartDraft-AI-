<script setup lang="ts">
import { onUnmounted, reactive, ref } from "vue";
import { auth } from "../auth/session";
import { ApiError } from "../auth/client";
import { productFields } from "../api/catalog";
import { productText, type Product } from "../api/orders";

const props = defineProps<{ product: Product; blocked?: boolean }>();
const emit = defineEmits<{ changed: []; busy: [value: boolean] }>();
const editing = ref(false),
  deleting = ref(false),
  acknowledged = ref(false);
const saving = ref(false),
  message = ref("");
const form = reactive({
  name: productText(props.product.name),
  description: productText(props.product.description),
  price: (props.product.price_cents / 100).toFixed(2),
  stock: String(props.product.stock),
});
const controller = new AbortController();
const pending = ref<{ path: string; method: string; body: string } | null>(
  null,
);

async function send() {
  if (saving.value || !pending.value) return;
  saving.value = true;
  emit("busy", true);
  message.value = "";
  try {
    await auth.request<Product>(pending.value.path, {
      method: pending.value.method,
      body: pending.value.body,
      signal: controller.signal,
    });
    if (!controller.signal.aborted) {
      pending.value = null;
      emit("changed");
    }
  } catch (error) {
    if (controller.signal.aborted) return;
    message.value = error instanceof Error ? error.message : "操作结果尚未确认";
    if (
      error instanceof ApiError &&
      error.status >= 400 &&
      error.status < 500
    ) {
      pending.value = null;
      message.value += "。请刷新目录后重新操作。";
    } else message.value += "。请重试原操作，系统不会重复执行。";
  } finally {
    saving.value = false;
    emit("busy", !!pending.value);
  }
}
function act(action: "edit" | "state" | "delete" | "restore") {
  if (
    saving.value ||
    props.blocked ||
    pending.value ||
    (action === "delete" && !acknowledged.value)
  )
    return;
  try {
    const payload = {
      operation_id: crypto.randomUUID(),
      expected_version: props.product.version,
      ...(action === "edit" ? productFields(form) : {}),
      ...(action === "state" ? { is_active: !props.product.is_active } : {}),
    };
    const suffix =
      action === "state" || action === "restore" ? `/${action}` : "";
    pending.value = {
      path: `/api/admin/catalog/products/${props.product.id}${suffix}`,
      method:
        action === "edit" ? "PATCH" : action === "delete" ? "DELETE" : "POST",
      body: JSON.stringify(payload),
    };
    void send();
  } catch (error) {
    message.value = error instanceof Error ? error.message : "输入不符合要求";
  }
}
onUnmounted(() => {
  controller.abort();
  emit("busy", false);
});
</script>
<template>
  <div class="product-actions">
    <div class="cancellation-actions">
      <template v-if="!product.deleted_at"
        ><button
          class="text-button"
          :disabled="saving || !!pending || blocked"
          @click="
            editing = !editing;
            deleting = false;
          "
        >
          {{ editing ? "收起编辑" : "编辑" }}</button
        ><button
          class="text-button"
          :disabled="saving || !!pending || blocked"
          @click="act('state')"
        >
          {{ product.is_active ? "下架" : "上架" }}</button
        ><button
          class="text-button"
          :disabled="saving || !!pending || blocked"
          @click="
            deleting = !deleting;
            editing = false;
          "
        >
          删除
        </button></template
      >
      <button
        v-else
        class="text-button"
        :disabled="saving || !!pending || blocked"
        @click="act('restore')"
      >
        恢复商品
      </button>
    </div>
    <form
      v-if="editing"
      class="product-edit-form"
      @submit.prevent="act('edit')"
    >
      <fieldset :disabled="saving || !!pending || blocked">
        <label
          >商品名称<input v-model="form.name" maxlength="120" required /></label
        ><label
          >商品说明<textarea
            v-model="form.description"
            maxlength="1000"
            rows="3"
            required
          />
        </label>
        <div class="product-edit-numbers">
          <label
            >售价（元）<input
              v-model="form.price"
              inputmode="decimal"
              required /></label
          ><label
            >可用库存<input v-model="form.stock" inputmode="numeric" required
          /></label>
        </div>
        <button class="button button-primary">保存修改</button>
      </fieldset>
    </form>
    <div v-if="deleting" class="product-delete-confirm">
      <p>
        删除后商品进入回收站，用户不可见；历史订单与库存记录保留，可随时恢复。
      </p>
      <label
        ><input
          v-model="acknowledged"
          type="checkbox"
          :disabled="saving || !!pending || blocked"
        />确认将此商品移入回收站</label
      ><button
        class="button button-outline"
        :disabled="!acknowledged || saving || !!pending || blocked"
        @click="act('delete')"
      >
        确认删除
      </button>
    </div>
    <p v-if="saving" role="status">正在保存…</p>
    <p v-if="message" class="notice notice-warning" role="alert">
      {{ message }}
    </p>
    <button
      v-if="pending && !saving"
      class="button button-outline"
      @click="send"
    >
      重试原操作
    </button>
  </div>
</template>
<style scoped>
.product-actions {
  margin-top: 12px;
  font-size: 12px;
}
.product-edit-form,
.product-delete-confirm {
  padding: 18px;
  margin-top: 15px;
  background: #f7f8fa;
  border: 1px solid var(--line);
  border-radius: 8px;
}
fieldset {
  border: 0;
  padding: 0;
  min-width: 0;
}
.product-edit-form label {
  display: grid;
  gap: 6px;
  margin-bottom: 12px;
}
.product-edit-form input,
textarea {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 9px;
  font: inherit;
  background: white;
}
.product-edit-numbers {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.product-delete-confirm label {
  display: block;
  margin: 12px 0;
}
</style>
