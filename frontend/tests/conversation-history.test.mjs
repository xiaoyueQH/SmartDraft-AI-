import assert from "node:assert/strict";
import { test } from "node:test";
import { component, dom, settle, vue } from "./sfc-support.mjs";

test("history filters the current customer and project, paginates and loads original context", async () => {
  const calls = [];
  const row = {
    id: "m1",
    project_id: "p1",
    content: "不要会员",
    sender_type: "customer",
    sender_name: "客户甲",
  };
  const View = component("../src/components/ConversationHistory.vue", {
    auth: {
      request: async (path) => {
        calls.push(path);
        return path.endsWith("/context")
          ? { items: [row] }
          : {
              items: [row],
              has_more: !path.includes("cursor="),
              next_cursor: "next",
            };
      },
    },
  });
  const root = document.createElement("div");
  document.body.append(root);
  const app = vue.createApp({
    render: () => vue.h(View, { projectId: "p1", customerId: "c1" }),
  });
  app.mount(root);
  const button = (text) =>
    [...root.querySelectorAll("button")].find((b) =>
      b.textContent.includes(text),
    );
  try {
    button("查询聊天记录").click();
    await settle();
    assert.match(calls[0], /customer_id=c1/);
    assert.match(calls[0], /project_id=p1/);
    assert.match(root.textContent, /客户甲/);
    button("定位原话").click();
    await settle();
    assert.equal(calls.at(-1), "/api/projects/p1/messages/m1/context");
    assert.match(
      root.querySelector(".history-context").textContent,
      /不要会员/,
    );
    button("加载更早记录").click();
    await settle();
    assert.match(calls.at(-1), /cursor=next/);
    const checkbox = root.querySelector('input[type="checkbox"]');
    checkbox.checked = true;
    checkbox.dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    root
      .querySelector("form")
      .dispatchEvent(
        new dom.window.Event("submit", { bubbles: true, cancelable: true }),
      );
    await settle();
    assert.ok(!calls.at(-1).includes("project_id="));
  } finally {
    app.unmount();
    root.remove();
  }
});
