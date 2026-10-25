import assert from "node:assert/strict";
import { test } from "node:test";
import * as client from "../src/auth/client.ts";
import { component, dom, settle, vue } from "./sfc-support.mjs";

const row = (id) => ({
  id,
  title: `需求沟通 ${id}`,
  phase: "discovery",
  version: 1,
  revision: 0,
  created_at: "2026-10-25T00:00:00Z",
  deleted_at: null,
});
function button(root, text) {
  return [...root.querySelectorAll("button")].find((node) =>
    node.textContent.includes(text),
  );
}
function mount(request, admin = false) {
  const root = document.createElement("div");
  document.body.append(root);
  const View = component(
    "../src/components/RecordManager.vue",
    { auth: { request } },
    { "../auth/client": client },
  );
  const changes = [];
  const app = vue.createApp(View, {
    admin,
    onChanged: (items) => changes.push(items),
  });
  const instance = app.mount(root);
  instance.open();
  return {
    root,
    changes,
    close() {
      app.unmount();
      root.remove();
    },
  };
}

test("batch deletion requires acknowledgement, freezes unknown results and retries identical payload", async () => {
  const active = [row("one"), row("two")],
    writes = [];
  let archived = false;
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes.push(init.body);
      if (writes.length === 1) throw new client.ApiError("网络中断", 0);
      archived = true;
      return {
        items: active.map((item) => ({
          ...item,
          version: 2,
          deleted_at: "2026-10-25T01:00:00Z",
        })),
      };
    }
    return {
      items: path.includes("deleted=true")
        ? archived
          ? active
          : []
        : archived
          ? []
          : active,
      has_more: false,
    };
  });
  try {
    await settle();
    const all = view.root.querySelector(
      'input[aria-label="全选本页可管理记录"]',
    );
    all.checked = true;
    all.dispatchEvent(new dom.window.Event("change"));
    await settle();
    button(view.root, "删除所选").click();
    await settle();
    assert.ok(button(view.root, "确认删除").disabled);
    assert.equal(writes.length, 0);
    const ack = view.root.querySelector(".record-ack input");
    ack.checked = true;
    ack.dispatchEvent(new dom.window.Event("change"));
    await settle();
    button(view.root, "确认删除").click();
    await settle();
    assert.equal(JSON.parse(writes[0]).targets.length, 2);
    assert.ok(button(view.root, "关闭").disabled);
    assert.ok(button(view.root, "返回列表").disabled);
    assert.ok(view.root.querySelector(".record-ack input").disabled);
    button(view.root, "重试原操作").click();
    await settle();
    assert.equal(writes.length, 2);
    assert.equal(writes[0], writes[1]);
    assert.equal(view.changes.length, 1);
    assert.match(view.root.textContent, /暂无记录/);
    button(view.root, "回收站").click();
    await settle();
    assert.ok(button(view.root, "恢复所选"));
  } finally {
    view.close();
  }
});

test("ordinary customer cannot select or delete a formal project", async () => {
  const view = mount(async () => ({
    items: [{ ...row("formal"), phase: "project", revision: 1 }],
  }));
  try {
    await settle();
    const selected = view.root.querySelector(
      'input[aria-label="选择记录 formal"]',
    );
    assert.ok(selected.disabled);
    assert.match(view.root.textContent, /负责人管理/);
    assert.equal(button(view.root, "重命名"), undefined);
  } finally {
    view.close();
  }
});

test("rename submits the visible version and does not modify a requirement document", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes.push(JSON.parse(init.body));
      return { items: [{ ...row("one"), title: "预约系统沟通", version: 2 }] };
    }
    return { items: [row("one")] };
  });
  try {
    await settle();
    button(view.root, "重命名").click();
    await settle();
    const input = view.root.querySelector('form input:not([type="checkbox"])');
    input.value = "预约系统沟通";
    input.dispatchEvent(new dom.window.Event("input"));
    await settle();
    view.root
      .querySelector("form")
      .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
    await settle();
    assert.equal(writes[0].action, "rename");
    assert.equal(writes[0].title, "预约系统沟通");
    assert.deepEqual(writes[0].targets, [{ id: "one", expected_version: 1 }]);
  } finally {
    view.close();
  }
});

test("restoring a record also requires checking the confirmation", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes.push(JSON.parse(init.body));
      return { items: [row("one")] };
    }
    return {
      items: path.includes("deleted=true")
        ? [{ ...row("one"), deleted_at: "2026-10-25T01:00:00Z" }]
        : [],
    };
  });
  try {
    await settle();
    button(view.root, "回收站").click();
    await settle();
    const restore = [...view.root.querySelectorAll("button")].find(
      (node) => node.textContent.trim() === "恢复",
    );
    restore.click();
    await settle();
    assert.ok(button(view.root, "确认恢复").disabled);
    const ack = view.root.querySelector(".record-ack input");
    ack.checked = true;
    ack.dispatchEvent(new dom.window.Event("change"));
    await settle();
    button(view.root, "确认恢复").click();
    await settle();
    assert.equal(writes[0].action, "restore");
  } finally {
    view.close();
  }
});
