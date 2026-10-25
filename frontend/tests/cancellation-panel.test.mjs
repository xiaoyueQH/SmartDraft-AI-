import assert from "node:assert/strict";
import { test } from "node:test";
import * as orders from "../src/api/orders.ts";
import * as client from "../src/auth/client.ts";
import { component, dom, settle, vue } from "./sfc-support.mjs";

const order = {
  id: "order-1",
  number: "SF-001",
  status: "paid",
  total_cents: 1000,
  is_demo: true,
  items: [],
  shipments: [],
};
const quote = {
  confirmation_id: "confirmation-1",
  order_id: order.id,
  number: order.number,
  from_status: "paid",
  reason: "不再需要",
  total_cents: 1000,
  expires_at: "2026-10-25T12:00:00Z",
  release_items: [
    { product_id: "product-1", product_name: "演示商品", quantity: 1 },
  ],
  refund_status: "simulated",
  refund_cents: 1000,
  is_demo: true,
};
const receipt = {
  ...quote,
  actor_username: "user",
  actor_role: "user",
  completed_at: "2026-10-25T11:56:00Z",
  released_units: 1,
  released_items: quote.release_items,
};
function mount(request, props = {}) {
  const root = document.createElement("div");
  document.body.append(root);
  const emitted = [];
  const Panel = component(
    "../src/components/CancelOrderPanel.vue",
    { auth: { request } },
    { "../api/orders": orders, "../auth/client": client },
  );
  const app = vue.createApp(Panel, {
    order,
    onCancelled: (result) => emitted.push(result),
    ...props,
  });
  app.mount(root);
  return {
    root,
    emitted,
    close() {
      app.unmount();
      root.remove();
    },
  };
}
const button = (root, label) =>
  [...root.querySelectorAll("button")].find(
    (node) => node.textContent.trim() === label,
  );
async function prepare(view) {
  const input = view.root.querySelector("textarea");
  input.value = "不再需要";
  input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
  view.root
    .querySelector("form")
    .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
  await settle();
}
async function acknowledge(view) {
  const checkbox = view.root.querySelector('input[type="checkbox"]');
  checkbox.checked = true;
  checkbox.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
  await settle();
}

test("cancelling requires preview and explicit acknowledgement before one write", async () => {
  const calls = [];
  let finish;
  const pending = new Promise((resolve) => {
    finish = resolve;
  });
  const view = mount(async (path, init) => {
    calls.push({ path, body: JSON.parse(init.body) });
    return path.endsWith("preview") ? quote : pending;
  });
  try {
    await prepare(view);
    assert.equal(calls.length, 1);
    assert.equal(button(view.root, "确认取消订单").disabled, true);
    assert.match(view.root.textContent, /不会发生真实资金退回/);
    await acknowledge(view);
    button(view.root, "确认取消订单").click();
    await settle();
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[1].body, {
      confirmation_id: quote.confirmation_id,
      confirmed: true,
    });
    assert.equal(button(view.root, "正在处理…").disabled, true);
    finish(receipt);
    await settle();
    assert.equal(view.emitted.length, 1);
    assert.match(view.root.textContent, /取消已完成/);
    assert.match(view.root.textContent, /没有向支付渠道发起真实退款/);
    assert.equal(view.root.querySelector("textarea"), null);
  } finally {
    view.close();
  }
});

test("uncertain network result retains the original confirmation for manual retry", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (path.endsWith("preview")) return quote;
    writes.push(JSON.parse(init.body));
    if (writes.length === 1) throw new TypeError("Network unavailable");
    return receipt;
  });
  try {
    await prepare(view);
    await acknowledge(view);
    button(view.root, "确认取消订单").click();
    await settle();
    assert.equal(writes.length, 1, "must never retry a write automatically");
    assert.match(
      view.root.querySelector('[role="alert"]').textContent,
      /结果尚未确认/,
    );
    button(view.root, "查询 / 重试原操作").click();
    await settle();
    assert.deepEqual(writes[0], writes[1]);
    assert.equal(view.emitted.length, 1);
  } finally {
    view.close();
  }
});

test("an expired or changed confirmation requires a new preview", async () => {
  const view = mount(async (path) => {
    if (path.endsWith("preview")) return quote;
    throw new client.ApiError(
      "取消确认已过期，请重新预览",
      409,
      "CONFIRMATION_EXPIRED",
    );
  });
  try {
    await prepare(view);
    await acknowledge(view);
    button(view.root, "确认取消订单").click();
    await settle();
    assert.equal(view.root.querySelector('[aria-label="取消确认"]'), null);
    assert.match(
      view.root.querySelector('[role="alert"]').textContent,
      /已过期/,
    );
    assert.ok(button(view.root, "检查取消条件"));
    assert.equal(view.emitted.length, 0);
  } finally {
    view.close();
  }
});

for (const patch of [
  { status: "shipped" },
  { status: "delivered" },
  { status: "paid", is_demo: false },
]) {
  test(`unsupported order ${JSON.stringify(patch)} never offers a cancellation button`, async () => {
    let calls = 0;
    const view = mount(
      async () => {
        calls++;
      },
      { order: { ...order, ...patch } },
    );
    try {
      await settle();
      assert.equal(view.root.querySelector("button"), null);
      assert.equal(calls, 0);
    } finally {
      view.close();
    }
  });
}

test("persisted cancellation audit is visible after reopening order details", async () => {
  const view = mount(
    async () => {
      throw new Error("must not submit");
    },
    { order: { ...order, status: "cancelled", cancellation: receipt } },
  );
  try {
    await settle();
    assert.match(view.root.textContent, /取消已完成/);
    assert.match(view.root.textContent, /confirmation-1/);
    assert.match(view.root.textContent, /不再需要/);
    assert.equal(view.root.querySelector("button"), null);
  } finally {
    view.close();
  }
});
