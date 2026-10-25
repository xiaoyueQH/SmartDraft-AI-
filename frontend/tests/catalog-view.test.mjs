import assert from "node:assert/strict";
import { test } from "node:test";

import * as orders from "../src/api/orders.ts";
import * as catalog from "../src/api/catalog.ts";
import * as client from "../src/auth/client.ts";
import { component, dom, settle, vue } from "./sfc-support.mjs";

const candidate = (id = "GPU-001") => ({
  id,
  name: `${id} 模拟显卡`,
  category: "显卡",
  description: "虚构商品参数，仅用于模拟",
  specifications: { 显存: "8GB" },
  suggested_price_cents: 19999,
  source: "mock_catalog_v1",
  is_demo: true,
  imported_product_id: null,
});
const page = {
  items: [candidate(), candidate("GPU-002")],
  categories: ["显卡", "耳机"],
  has_more: true,
};
const product = {
  id: "product-one",
  sku: "MOCK-GPU-001",
  name: "模拟显卡",
  description: "模拟商品",
  price_cents: 19999,
  stock: 7,
  is_demo: true,
  is_active: true,
  deleted_at: null,
  version: 1,
};
const button = (root, text) =>
  [...root.querySelectorAll("button")].find((node) =>
    node.textContent.includes(text),
  );
function mount(request) {
  const root = document.createElement("div");
  document.body.append(root);
  const View = component(
    "../src/views/CatalogView.vue",
    { auth: { request } },
    {
      "../api/orders": orders,
      "../api/catalog": catalog,
      "../auth/client": client,
    },
  );
  const app = vue.createApp(View);
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
function submit(node) {
  node.dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
}

test("catalog import submits edited values with exact integer cents and marks imported candidate", async () => {
  const requests = [];
  const view = mount(async (path, init) => {
    if (init.method === "POST") {
      requests.push(JSON.parse(init.body));
      return { product, created: true };
    }
    return structuredClone(page);
  });
  try {
    await settle();
    assert.match(view.root.textContent, /演示环境/);
    button(view.root, "选择入库").click();
    await settle();
    const editor = view.root.querySelector(".catalog-editor");
    const inputs = editor.querySelectorAll("input");
    enter(inputs[0], "编辑后的模拟显卡");
    enter(inputs[1], "199.99");
    enter(inputs[2], "7");
    submit(editor.querySelector("form"));
    await settle();
    assert.deepEqual(requests[0], {
      candidate_id: "GPU-001",
      name: "编辑后的模拟显卡",
      description: "虚构商品参数，仅用于模拟\n显存：8GB",
      price_cents: 19999,
      stock: 7,
    });
    assert.equal(button(view.root, "已入库 ✓").disabled, true);
    assert.match(view.root.textContent, /用户商品目录可见/);
    assert.equal(view.root.querySelector(".catalog-editor"), null);
  } finally {
    view.close();
  }
});

test("manual entry uses the modal without moving candidate cards and saves custom SKU", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (init.method === "POST") {
      writes.push({ path, body: JSON.parse(init.body) });
      return {
        product: { ...product, sku: "OWN-001", name: "自有商品" },
        created: true,
      };
    }
    return path.includes("candidates")
      ? structuredClone(page)
      : { items: [product], has_more: false };
  });
  try {
    await settle();
    const cards = [...view.root.querySelectorAll(".catalog-card")];
    button(view.root, "新增商品").click();
    await settle();
    const dialog = view.root.querySelector("dialog[open]");
    assert.ok(dialog);
    assert.equal(view.root.querySelector(".has-editor"), null);
    assert.deepEqual([...view.root.querySelectorAll(".catalog-card")], cards);
    const inputs = dialog.querySelectorAll("input");
    enter(inputs[0], "own-001");
    enter(inputs[1], "自有商品");
    enter(dialog.querySelector("textarea"), "我自己的商品说明");
    enter(inputs[2], "99.99");
    enter(inputs[3], "15");
    submit(dialog.querySelector("form"));
    await settle();
    assert.equal(writes[0].path, "/api/admin/catalog/products");
    assert.equal(writes[0].body.sku, "OWN-001");
    assert.equal(writes[0].body.price_cents, 9999);
    assert.equal(writes[0].body.stock, 15);
    assert.ok(writes[0].body.request_id);
    assert.equal(view.root.querySelector("dialog"), null);
    assert.ok(view.root.querySelector(".catalog-saved"));
  } finally {
    view.close();
  }
});

test("uncertain manual submission keeps immutable request and blocks closing until retry", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (init.method !== "POST")
      return path.includes("candidates")
        ? structuredClone(page)
        : { items: [], has_more: false };
    writes.push(init.body);
    if (writes.length === 1) throw new Error("结果未知");
    return { product, created: false };
  });
  try {
    await settle();
    button(view.root, "新增商品").click();
    await settle();
    const dialog = view.root.querySelector("dialog");
    const inputs = dialog.querySelectorAll("input");
    enter(inputs[0], "OWN-001");
    enter(inputs[1], "自有商品");
    enter(dialog.querySelector("textarea"), "商品说明");
    enter(inputs[2], "199.99");
    submit(dialog.querySelector("form"));
    await settle();
    assert.equal(button(view.root, "关闭").disabled, true);
    assert.equal(dialog.querySelector("fieldset").disabled, true);
    dialog.dispatchEvent(new dom.window.Event("cancel", { cancelable: true }));
    await settle();
    assert.ok(dialog.open);
    button(view.root, "重试原入库请求").click();
    await settle();
    assert.equal(writes[0], writes[1]);
    assert.equal(view.root.querySelector("dialog"), null);
  } finally {
    view.close();
  }
});

test("candidate modal closes with Escape without replacing grid nodes", async () => {
  const view = mount(async () => structuredClone(page));
  try {
    await settle();
    const first = view.root.querySelector(".catalog-card");
    button(view.root, "选择入库").click();
    await settle();
    assert.ok(view.root.querySelector("dialog[open]"));
    view.root
      .querySelector("dialog")
      .dispatchEvent(new dom.window.Event("cancel", { cancelable: true }));
    await settle();
    assert.equal(view.root.querySelector("dialog"), null);
    assert.equal(view.root.querySelector(".catalog-card"), first);
  } finally {
    view.close();
  }
});

test("pending import blocks double submit and failure retains candidate for idempotent manual retry", async () => {
  let reject,
    calls = 0;
  const bodies = [];
  const view = mount(async (path, init) => {
    if (init.method !== "POST") return structuredClone(page);
    calls++;
    bodies.push(init.body);
    if (calls === 1)
      return new Promise((_, fail) => {
        reject = fail;
      });
    return { product, created: false };
  });
  try {
    await settle();
    button(view.root, "选择入库").click();
    await settle();
    const form = view.root.querySelector(".catalog-editor form");
    submit(form);
    submit(form);
    await settle();
    assert.equal(calls, 1);
    assert.equal(form.querySelector("fieldset").disabled, true);
    reject(new Error("网络暂时断开"));
    await settle();
    assert.match(
      view.root.querySelector('[role="alert"]').textContent,
      /可重试同一商品/,
    );
    assert.ok(view.root.querySelector(".catalog-editor"));
    submit(form);
    await settle();
    assert.equal(bodies[0], bodies[1]);
    assert.match(view.root.textContent, /保留首次入库的售价和库存/);
  } finally {
    view.close();
  }
});

test("invalid decimal or fractional stock never reaches import endpoint", async () => {
  let writes = 0;
  const view = mount(async (_, init) => {
    if (init.method === "POST") writes++;
    return structuredClone(page);
  });
  try {
    await settle();
    button(view.root, "选择入库").click();
    await settle();
    const form = view.root.querySelector(".catalog-editor form");
    const inputs = form.querySelectorAll("input");
    for (const [price, stock] of [
      ["1.001", "1"],
      ["1", "1.5"],
      ["-2", "1"],
      ["0", "1"],
      ["2", "1000001"],
    ]) {
      enter(inputs[1], price);
      enter(inputs[2], stock);
      submit(form);
      await settle();
      assert.ok(view.root.querySelector('[role="alert"]'));
    }
    assert.equal(writes, 0);
  } finally {
    view.close();
  }
});

test("catalog pagination uses submitted filter and tab change discards late candidate responses", async () => {
  const requests = [];
  let pending;
  const view = mount(async (path, init) => {
    requests.push(path);
    if (path.includes("products?"))
      return { items: [product], has_more: false };
    if (requests.length === 4)
      return new Promise((resolve) => {
        pending = { signal: init.signal, resolve };
      });
    return structuredClone(page);
  });
  try {
    await settle();
    enter(view.root.querySelector(".catalog-search input"), "显卡");
    button(view.root, "下一页").click();
    await settle();
    assert.equal(
      new URL(requests.at(-1), "http://localhost").searchParams.get("q"),
      "",
    );
    submit(view.root.querySelector(".catalog-search"));
    await settle();
    assert.equal(
      new URL(requests.at(-1), "http://localhost").searchParams.get("q"),
      "显卡",
    );
    assert.equal(
      new URL(requests.at(-1), "http://localhost").searchParams.get("offset"),
      "0",
    );
    submit(view.root.querySelector(".catalog-search"));
    await settle();
    button(view.root, "已入库目录").click();
    await settle();
    assert.equal(pending.signal.aborted, true);
    pending.resolve(structuredClone(page));
    await settle();
    assert.equal(view.root.querySelectorAll(".catalog-card").length, 0);
    assert.match(
      view.root.querySelector(".catalog-saved").textContent,
      /MOCK-GPU-001/,
    );
  } finally {
    view.close();
  }
});

test("unmount cancels in-flight import and late completion cannot change another account page", async () => {
  let pending;
  const view = mount(async (_, init) => {
    if (init.method === "POST")
      return new Promise((resolve) => {
        pending = { signal: init.signal, resolve };
      });
    return structuredClone(page);
  });
  await settle();
  button(view.root, "选择入库").click();
  await settle();
  submit(view.root.querySelector(".catalog-editor form"));
  await settle();
  view.close();
  assert.equal(pending.signal.aborted, true);
  pending.resolve({ product, created: true });
  await settle();
  assert.equal(document.body.textContent, "");
});
