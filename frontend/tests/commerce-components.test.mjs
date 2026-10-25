import assert from "node:assert/strict";
import { test } from "node:test";

import * as orders from "../src/api/orders.ts";
import * as catalog from "../src/api/catalog.ts";
import * as client from "../src/auth/client.ts";
import { component, dom, settle, vue } from "./sfc-support.mjs";

const product = {
  id: "product-one",
  sku: "GPU-001",
  name: "星澜 G60",
  description: "图形处理系列",
  price_cents: 19999,
  stock: 5,
  is_demo: true,
  is_active: true,
  version: 2,
  deleted_at: null,
};
dom.window.HTMLElement.prototype.scrollIntoView = function () {};
const button = (root, text) =>
  [...root.querySelectorAll("button")].find((node) =>
    node.textContent.includes(text),
  );
function mount(file, request, props = {}, events = {}) {
  const root = document.createElement("div");
  document.body.append(root);
  const View = component(
    file,
    { auth: { request } },
    {
      "../api/orders": orders,
      "../api/catalog": catalog,
      "../auth/client": client,
      "vue-router": {
        RouterLink: {
          props: ["to"],
          setup:
            (props, { slots }) =>
            () =>
              vue.h("a", { href: props.to }, slots.default?.()),
        },
      },
    },
  );
  const app = vue.createApp(View, {
    product: structuredClone(product),
    ...props,
    ...events,
  });
  app.mount(root);
  return {
    root,
    close: () => {
      app.unmount();
      root.remove();
    },
  };
}
function enter(input, value) {
  input.value = value;
  input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
}
function submit(form) {
  form.dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
}

test("order creation needs confirmation, snapshots price and retries original request after uncertain network result", async () => {
  const requests = [];
  let completed = 0;
  const view = mount(
    "../src/components/CreateOrderPanel.vue",
    async (_, init) => {
      requests.push(init.body);
      if (requests.length === 1) throw new Error("连接断开");
      return {
        created: false,
        order: { id: "order1", number: "SF-ONE", total_cents: 39998 },
      };
    },
    {},
    { onCompleted: () => completed++ },
  );
  try {
    await settle();
    assert.equal(button(view.root, "确认下单").disabled, true);
    enter(view.root.querySelector('input[type="number"]'), "2");
    const checkbox = view.root.querySelector('input[type="checkbox"]');
    checkbox.click();
    await settle();
    submit(view.root.querySelector("form"));
    await settle();
    assert.equal(JSON.parse(requests[0]).expected_price_cents, 19999);
    assert.equal(JSON.parse(requests[0]).quantity, 2);
    assert.match(
      view.root.querySelector('[role="alert"]').textContent,
      /同一请求/,
    );
    assert.equal(
      view.root.querySelector('input[type="number"]').disabled,
      true,
    );
    button(view.root, "重试原下单请求").click();
    await settle();
    assert.equal(requests.length, 2);
    assert.equal(requests[0], requests[1]);
    assert.equal(completed, 1);
    assert.match(view.root.textContent, /SF-ONE/);
    assert.match(view.root.textContent, /库存已预占/);
  } finally {
    view.close();
  }
});

test("checkout rejects changed-price conflict and requires fresh confirmation", async () => {
  const view = mount("../src/components/CreateOrderPanel.vue", async () => {
    throw new client.ApiError("价格已变更", 409, "PRICE_CHANGED");
  });
  try {
    view.root.querySelector('input[type="checkbox"]').click();
    await settle();
    submit(view.root.querySelector("form"));
    await settle();
    assert.equal(
      view.root.querySelector('input[type="checkbox"]').checked,
      false,
    );
    assert.equal(button(view.root, "确认下单").disabled, true);
    assert.match(view.root.textContent, /刷新商品目录/);
  } finally {
    view.close();
  }
});

test("product edit submits version and exact money; pending write prevents repeated submission", async () => {
  let pending;
  const requests = [];
  let changed = 0;
  const view = mount(
    "../src/components/ProductActions.vue",
    async (path, init) => {
      requests.push({ path, ...init });
      return new Promise((resolve) => {
        pending = resolve;
      });
    },
    {},
    { onChanged: () => changed++ },
  );
  try {
    button(view.root, "编辑").click();
    await settle();
    const inputs = view.root.querySelectorAll("form input");
    enter(inputs[0], "新名称");
    enter(inputs[1], "88.88");
    enter(inputs[2], "12");
    submit(view.root.querySelector("form"));
    submit(view.root.querySelector("form"));
    await settle();
    assert.equal(requests.length, 1);
    assert.equal(requests[0].method, "PATCH");
    const data = JSON.parse(requests[0].body);
    assert.equal(data.expected_version, 2);
    assert.equal(data.price_cents, 8888);
    assert.equal(data.stock, 12);
    pending(product);
    await settle();
    assert.equal(changed, 1);
  } finally {
    view.close();
  }
});

test("recoverable product deletion requires explicit checkbox and retries original operation", async () => {
  const requests = [];
  const view = mount(
    "../src/components/ProductActions.vue",
    async (_, init) => {
      requests.push(init.body);
      throw new Error("保存结果未知");
    },
  );
  try {
    button(view.root, "删除").click();
    await settle();
    assert.equal(button(view.root, "确认删除").disabled, true);
    view.root.querySelector('input[type="checkbox"]').click();
    await settle();
    button(view.root, "确认删除").click();
    await settle();
    button(view.root, "重试原操作").click();
    await settle();
    assert.equal(requests.length, 2);
    assert.equal(requests[0], requests[1]);
    assert.match(view.root.textContent, /回收站/);
  } finally {
    view.close();
  }
});

test("recycle-bin entries offer restoration instead of editing or sale", async () => {
  const requests = [];
  const view = mount(
    "../src/components/ProductActions.vue",
    async (path, init) => {
      requests.push({ path, init });
      return product;
    },
    {
      product: {
        ...product,
        deleted_at: "2026-10-25T00:00:00Z",
        is_active: false,
      },
    },
  );
  try {
    assert.equal(button(view.root, "编辑"), undefined);
    assert.equal(button(view.root, "上架"), undefined);
    button(view.root, "恢复商品").click();
    await settle();
    assert.ok(requests[0].path.endsWith("/restore"));
    assert.equal(requests[0].init.method, "POST");
  } finally {
    view.close();
  }
});

test("unmount aborts pending checkout and suppresses completion in another account", async () => {
  let pending;
  let completed = false;
  const view = mount(
    "../src/components/CreateOrderPanel.vue",
    async (_, init) =>
      new Promise((resolve) => {
        pending = { signal: init.signal, resolve };
      }),
    {},
    {
      onCompleted: () => {
        completed = true;
      },
    },
  );
  view.root.querySelector('input[type="checkbox"]').click();
  await settle();
  submit(view.root.querySelector("form"));
  await settle();
  view.close();
  assert.equal(pending.signal.aborted, true);
  pending.resolve({ order: { number: "late" }, created: true });
  await settle();
  assert.equal(completed, false);
});

test("product selection cannot discard an in-flight or uncertain checkout request", async () => {
  let pending;
  const writes = [];
  const view = mount("../src/views/ProductsView.vue", async (_, init) => {
    if (init.method !== "POST")
      return {
        items: [product, { ...product, id: "second", name: "另一个商品" }],
        has_more: false,
      };
    writes.push(init.body);
    if (writes.length === 1)
      return new Promise((resolve, reject) => {
        pending = { resolve, reject, signal: init.signal };
      });
    return {
      created: false,
      order: { number: "RECOVERED", total_cents: 19999 },
    };
  });
  try {
    await settle();
    button(view.root, "创建订单").click();
    await settle();
    view.root.querySelector('input[type="checkbox"]').click();
    await settle();
    submit(view.root.querySelector(".checkout-panel form"));
    await settle();
    const choices = [...view.root.querySelectorAll(".product-order-button")];
    assert.ok(choices.every((node) => node.disabled));
    choices[1].click();
    await settle();
    assert.equal(pending.signal.aborted, false);
    pending.reject(new Error("未知结果"));
    await settle();
    assert.ok(choices.every((node) => node.disabled));
    assert.equal(button(view.root, "收起").disabled, true);
    button(view.root, "重试原下单请求").click();
    await settle();
    assert.equal(writes[0], writes[1]);
    assert.match(view.root.textContent, /RECOVERED/);
  } finally {
    view.close();
  }
});

test("one catalog mutation blocks sibling operations until the original result is recovered", async () => {
  let pending;
  const writes = [];
  const view = mount("../src/views/CatalogView.vue", async (path, init) => {
    if (init.method !== "POST")
      return path.includes("candidates")
        ? { items: [], categories: [], has_more: false }
        : {
            items: [product, { ...product, id: "second", name: "另一件商品" }],
            has_more: false,
          };
    writes.push(init.body);
    if (writes.length === 1)
      return new Promise((resolve, reject) => {
        pending = { resolve, reject, signal: init.signal };
      });
    return product;
  });
  try {
    await settle();
    button(view.root, "已入库目录").click();
    await settle();
    const rows = view.root.querySelectorAll(".product-actions");
    button(rows[0], "下架").click();
    await settle();
    assert.equal(button(rows[1], "下架").disabled, true);
    button(rows[1], "下架").click();
    await settle();
    assert.equal(writes.length, 1);
    assert.equal(pending.signal.aborted, false);
    pending.reject(new Error("未知结果"));
    await settle();
    assert.equal(button(rows[1], "下架").disabled, true);
    assert.equal(button(view.root, "回收站").disabled, true);
    button(rows[0], "重试原操作").click();
    await settle();
    assert.equal(writes[0], writes[1]);
    assert.equal(button(view.root, "回收站").disabled, false);
  } finally {
    view.close();
  }
});
