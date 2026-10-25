import assert from "node:assert/strict";
import { test } from "node:test";
import * as orders from "../src/api/orders.ts";
import * as client from "../src/auth/client.ts";
import * as workflow from "../src/api/workflow.ts";
import { component, dom, settle, vue } from "./sfc-support.mjs";

const order = {
  id: "one",
  number: "SF-ONE",
  status: "pending_payment",
  customer_username: "alice",
  total_cents: 99900,
  is_demo: true,
  created_at: "2026-10-25T00:00:00Z",
  items: [],
  shipments: [],
  events: [],
  after_sale: null,
};
const button = (root, text) =>
  [...root.querySelectorAll("button")].find((node) =>
    node.textContent.includes(text),
  );
function mount(
  request,
  props = {},
  file = "../src/components/OrderBusinessPanel.vue",
  events = {},
) {
  const guards = [];
  const identity = { request, user: { id: "alice" } };
  const root = document.createElement("div");
  document.body.append(root);
  const View = component(
    file,
    { auth: identity },
    {
      "../api/orders": orders,
      "../auth/client": client,
      "../api/workflow": workflow,
      "vue-router": {
        useRoute: () => ({ query: {} }),
        onBeforeRouteLeave: (guard) => guards.push(guard),
        onBeforeRouteUpdate: (guard) => guards.push(guard),
        RouterLink: {
          props: ["to"],
          setup:
            (props, { slots }) =>
            () =>
              vue.h(
                "a",
                { "data-target": JSON.stringify(props.to) },
                slots.default?.(),
              ),
        },
      },
    },
  );
  const app = vue.createApp(View, {
    order: structuredClone(order),
    ...props,
    ...events,
  });
  app.mount(root);
  return {
    root,
    guards,
    identity,
    close: () => {
      app.unmount();
      root.remove();
    },
  };
}
function input(node, value) {
  node.value = value;
  node.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
}
function submit(node) {
  node.dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
}

test("payment needs explicit acknowledgement and retries identical operation after uncertain response", async () => {
  const requests = [];
  const busy = [];
  const view = mount(
    async (path, init) => {
      requests.push({ path, body: init.body });
      if (requests.length === 1) throw new Error("网络断开");
      return { order_id: "one", order_status: "paid" };
    },
    {},
    undefined,
    { onBusy: (value) => busy.push(value) },
  );
  try {
    button(view.root, "确认演示付款").click();
    await settle();
    const form = view.root.querySelector(".business-confirm");
    assert.equal(button(form, "确认办理").disabled, true);
    form.querySelector("input[type=checkbox]").click();
    await settle();
    submit(form);
    await settle();
    assert.equal(button(form, "返回").disabled, true);
    assert.ok(busy.at(-1));
    button(form, "重试原操作").click();
    await settle();
    assert.equal(requests[0].body, requests[1].body);
    assert.equal(requests[0].path, "/api/orders/one/pay");
    assert.equal(JSON.parse(requests[0].body).confirmed, true);
  } finally {
    view.close();
  }
});

test("admin shipping submits carrier and tracking with confirmation", async () => {
  const calls = [];
  const view = mount(
    async (path, init) => {
      calls.push({ path, body: JSON.parse(init.body) });
      return { order_id: "one", order_status: "shipped" };
    },
    { admin: true, order: { ...order, status: "paid" } },
  );
  try {
    button(view.root, "登记发货").click();
    await settle();
    const form = view.root.querySelector(".business-confirm");
    const fields = form.querySelectorAll("input:not([type=checkbox])");
    input(fields[0], "演示快递");
    input(fields[1], "SF-FWD-001");
    form.querySelector("input[type=checkbox]").click();
    await settle();
    submit(form);
    await settle();
    assert.equal(calls[0].path, "/api/admin/orders/one/ship");
    assert.equal(calls[0].body.tracking_number, "SF-FWD-001");
  } finally {
    view.close();
  }
});

test("return review supports rejection reason and shows saved case facts", async () => {
  const calls = [];
  const view = mount(
    async (path, init) => {
      calls.push(JSON.parse(init.body));
      return { order_id: "one", order_status: "delivered" };
    },
    {
      admin: true,
      order: {
        ...order,
        status: "delivered",
        after_sale: {
          id: "case",
          status: "requested",
          reason: "申请整单退货",
          refund_cents: 99900,
          review_note: "",
        },
      },
    },
  );
  try {
    assert.match(view.root.textContent, /待审核/);
    button(view.root, "审核退货申请").click();
    await settle();
    const form = view.root.querySelector(".business-confirm");
    const select = form.querySelector("select");
    select.value = "reject";
    select.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    input(form.querySelector("textarea"), "信息不完整，暂不受理");
    form.querySelector("input[type=checkbox]").click();
    await settle();
    submit(form);
    await settle();
    assert.equal(calls[0].decision, "reject");
    assert.equal(calls[0].reason, "信息不完整，暂不受理");
  } finally {
    view.close();
  }
});

test("a workflow draft and unknown result prevent order switching and cancellation", async () => {
  let reject;
  const writes = [];
  const view = mount(
    async (path, init) => {
      if (init.method === "POST") {
        writes.push(init.body);
        return new Promise((_, fail) => {
          reject = fail;
        });
      }
      if (path.includes("?"))
        return {
          items: [order, { ...order, id: "two", number: "SF-TWO" }],
          has_more: false,
        };
      return structuredClone(order);
    },
    {},
    "../src/views/OrdersView.vue",
  );
  try {
    await settle();
    button(view.root, "查看详情").click();
    await settle();
    button(view.root, "确认演示付款").click();
    await settle();
    assert.equal(button(view.root, "刷新").disabled, true);
    assert.equal(view.guards.length, 2);
    assert.ok(view.guards.every((guard) => guard() === false));
    assert.ok(
      [...view.root.querySelectorAll("button")]
        .filter((node) => node.textContent.includes("查看详情"))
        .every((node) => node.disabled),
    );
    assert.equal(
      view.root.querySelector(".cancellation-panel textarea").disabled,
      true,
    );
    const form = view.root.querySelector(".business-confirm");
    form.querySelector("input[type=checkbox]").click();
    await settle();
    submit(form);
    await settle();
    reject(new Error("未知结果"));
    await settle();
    assert.equal(writes.length, 1);
    assert.equal(button(view.root, "刷新").disabled, true);
    assert.equal(button(form, "返回").disabled, true);
    assert.ok(view.guards.every((guard) => guard() === false));
    view.identity.user = null;
    assert.ok(view.guards.every((guard) => guard() === true));
  } finally {
    view.close();
  }
});

test("a cancellation preview blocks simultaneous payment", async () => {
  const view = mount(async () => ({
    confirmation_id: "q1",
    number: "SF-ONE",
    from_status: "pending_payment",
    release_items: [],
    expires_at: "2026-10-25T20:00:00Z",
    total_cents: 99900,
  }));
  try {
    input(view.root.querySelector(".cancellation-panel textarea"), "不需要了");
    await settle();
    submit(view.root.querySelector(".cancellation-panel form"));
    await settle();
    assert.equal(button(view.root, "确认演示付款").disabled, true);
  } finally {
    view.close();
  }
});

test("returns list preserves submitted filter and links to scoped order detail", async () => {
  const paths = [];
  const view = mount(
    async (path) => {
      paths.push(path);
      return {
        items: [
          {
            id: "case",
            order_id: "one",
            number: "SF-ONE",
            customer_username: "alice",
            status: "requested",
            reason: "申请退货",
            refund_cents: 99900,
            created_at: "2026-10-25T00:00:00Z",
          },
        ],
        has_more: true,
      };
    },
    { admin: true },
    "../src/views/ReturnsView.vue",
  );
  try {
    await settle();
    const select = view.root.querySelector("select");
    select.value = "closed";
    select.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    button(view.root, "下一页").click();
    await settle();
    assert.equal(
      new URL(paths.at(-1), "http://local").searchParams.has("status"),
      false,
    );
    submit(view.root.querySelector("form"));
    await settle();
    assert.equal(
      new URL(paths.at(-1), "http://local").searchParams.get("status"),
      "closed",
    );
    assert.deepEqual(JSON.parse(view.root.querySelector("a").dataset.target), {
      path: "/admin/orders",
      query: { order_id: "one" },
    });
  } finally {
    view.close();
  }
});
