import assert from "node:assert/strict";
import { test } from "node:test";

import * as orders from "../src/api/orders.ts";
import * as client from "../src/auth/client.ts";
import * as workflow from "../src/api/workflow.ts";
import { component, dom, settle, vue } from "./sfc-support.mjs";

const summary = (id) => ({
  id,
  number: `ORDER-${id}`,
  customer_username: "alice",
  status: "shipped",
  total_cents: 12900,
  created_at: "2026-10-24T00:00:00Z",
  is_demo: true,
});
const page = { items: [summary("one"), summary("two")], has_more: true };
function mount(request, props = {}) {
  const root = document.createElement("div");
  document.body.append(root);
  const View = component(
    "../src/views/OrdersView.vue",
    { auth: { request } },
    {
      "../api/orders": orders,
      "../auth/client": client,
      "../api/workflow": workflow,
      "vue-router": {
        useRoute: () => ({ query: {} }),
        onBeforeRouteLeave: () => {},
        onBeforeRouteUpdate: () => {},
      },
    },
  );
  const app = vue.createApp(View, props);
  app.mount(root);
  return {
    root,
    close: () => {
      app.unmount();
      root.remove();
    },
  };
}

test("order pagination keeps submitted filters and a new search resets the page", async () => {
  const urls = [];
  const view = mount(
    async (path) => {
      urls.push(path);
      return page;
    },
    { admin: true },
  );
  try {
    await settle();
    assert.ok(urls[0].startsWith("/api/admin/orders?"));
    const select = view.root.querySelector("select");
    select.value = "paid";
    select.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    const next = [...view.root.querySelectorAll("button")].find(
      (node) => node.textContent.trim() === "下一页",
    );
    next.click();
    await settle();
    let query = new URL(urls.at(-1), "http://localhost").searchParams;
    assert.equal(query.get("offset"), "20");
    assert.equal(query.has("status"), false);
    view.root
      .querySelector("form")
      .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
    await settle();
    query = new URL(urls.at(-1), "http://localhost").searchParams;
    assert.equal(query.get("offset"), "0");
    assert.equal(query.get("status"), "paid");
    assert.match(view.root.textContent, /alice/);
  } finally {
    view.close();
  }
});

test("late cancelled order details cannot overwrite a more recent selection", async () => {
  const pending = [];
  const view = mount(async (path, init) => {
    if (path.includes("?")) return page;
    return new Promise((resolve) =>
      pending.push({ path, signal: init.signal, resolve }),
    );
  });
  try {
    await settle();
    const buttons = view.root.querySelectorAll(".text-button");
    buttons[0].click();
    await settle();
    buttons[1].click();
    await settle();
    assert.equal(pending[0].signal.aborted, true);
    pending[1].resolve({ ...summary("two"), items: [], shipments: [] });
    await settle();
    pending[0].resolve({ ...summary("one"), items: [], shipments: [] });
    await settle();
    assert.match(
      view.root.querySelector(".order-detail-number").textContent,
      /ORDER-two/,
    );
    assert.match(
      view.root.querySelector(".order-logistics").textContent,
      /暂无物流记录/,
    );
  } finally {
    view.close();
  }
});

test("a failed order search clears stale results and presents a retryable error", async () => {
  let calls = 0;
  const view = mount(async () => {
    if (++calls === 1) return page;
    throw new Error("连接暂时不可用");
  });
  try {
    await settle();
    view.root
      .querySelector("form")
      .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
    await settle();
    assert.equal(view.root.querySelectorAll("tbody tr").length, 0);
    assert.match(
      view.root.querySelector('[role="alert"]').textContent,
      /连接暂时不可用/,
    );
    assert.equal(
      view.root.querySelector('button[type="submit"]')?.disabled ??
        view.root.querySelector(".order-filters button").disabled,
      false,
    );
  } finally {
    view.close();
  }
});
