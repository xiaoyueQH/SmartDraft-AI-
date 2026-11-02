import assert from "node:assert/strict";
import { test } from "node:test";
import * as client from "../src/auth/client.ts";
import { component, settle, vue } from "./sfc-support.mjs";

const item = {
  id: "one",
  project_id: "p1",
  version: 3,
  content: "current",
  title: "需求",
  questions: [],
};
const latest = {
  can_undo: true,
  change: {
    id: "patch-one",
    edits: [
      {
        block_id: "b1",
        title: "规则",
        old_text: "<script>alert(1)</script>",
        new_text: "3小时",
      },
    ],
  },
};
function mount(handler) {
  const root = document.createElement("div");
  document.body.append(root);
  const props = vue.reactive({
    projectId: "p1",
    item: { ...item },
    locked: false,
  });
  const View = component(
    "../src/components/DocumentChange.vue",
    { auth: { request: handler } },
    { "../auth/client": client },
  );
  const changed = [],
    busy = [];
  const app = vue.createApp({
    render: () =>
      vue.h(View, {
        ...props,
        onChanged: (value) => changed.push(value),
        onBusy: (value) => busy.push(value),
      }),
  });
  app.mount(root);
  return {
    root,
    props,
    changed,
    busy,
    close() {
      app.unmount();
      root.remove();
    },
  };
}
function button(root, label) {
  return [...root.querySelectorAll("button")].find((b) =>
    b.textContent.includes(label),
  );
}

test("latest change renders escaped diff and undo uses current version", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (!init) return latest;
    writes.push(JSON.parse(init.body));
    return { item: { ...item, version: 4, content: "restored" } };
  });
  try {
    await settle();
    assert.match(view.root.textContent, /最近一次更新了 1/);
    assert.equal(view.root.querySelector("script"), null);
    assert.match(view.root.textContent, /<script>/);
    button(view.root, "撤销本次修改").click();
    await settle();
    assert.equal(writes[0].expected_version, 3);
    assert.equal(writes[0].change_id, "patch-one");
    assert.equal(view.changed[0].content, "restored");
    view.props.item = view.changed[0];
    await settle();
    assert.match(view.root.textContent, /已撤销最近一次修改/);
    assert.equal(view.busy.at(-1), false);
  } finally {
    view.close();
  }
});

test("unknown undo result retries identical request and blocks other edits", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (!init) return latest;
    writes.push(init.body);
    if (writes.length === 1) throw new client.ApiError("连接中断", 0);
    return { item: { ...item, version: 4 } };
  });
  try {
    await settle();
    button(view.root, "撤销本次修改").click();
    await settle();
    assert.equal(view.busy.at(-1), true);
    button(view.root, "原请求重试").click();
    await settle();
    assert.equal(writes[0], writes[1]);
    assert.equal(view.changed.length, 1);
    assert.equal(view.busy.at(-1), false);
  } finally {
    view.close();
  }
});

test("late undo acknowledgement cannot replace a newer document", async () => {
  let resolve;
  const view = mount(async (path, init) =>
    !init
      ? latest
      : await new Promise((r) => {
          resolve = r;
        }),
  );
  try {
    await settle();
    button(view.root, "撤销本次修改").click();
    await settle();
    view.props.item = { ...item, version: 6, content: "newer" };
    await settle();
    resolve({ item: { ...item, version: 4, content: "older restored" } });
    await settle();
    assert.equal(view.changed.length, 0);
    assert.match(view.root.textContent, /保留后来更新的内容/);
  } finally {
    view.close();
  }
});

test("switching project discards old diff and late mutation reply", async () => {
  let resolve;
  const view = mount(async (path, init) =>
    !init
      ? path.includes("p1")
        ? latest
        : { change: null, can_undo: false }
      : await new Promise((r) => {
          resolve = r;
        }),
  );
  try {
    await settle();
    button(view.root, "撤销本次修改").click();
    await settle();
    view.props.projectId = "p2";
    view.props.item = { ...item, id: "two", project_id: "p2" };
    await settle();
    resolve({ item: { ...item, version: 4 } });
    await settle();
    assert.equal(view.changed.length, 0);
    assert.equal(button(view.root, "原请求重试"), undefined);
    assert.equal(view.busy.at(-1), false);
  } finally {
    view.close();
  }
});
