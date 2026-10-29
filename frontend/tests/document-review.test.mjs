import assert from "node:assert/strict";
import { test } from "node:test";
import * as client from "../src/auth/client.ts";
import { component, dom, settle, vue } from "./sfc-support.mjs";

const item = {
  id: "item-one",
  project_id: "project-one",
  version: 3,
  status: "draft",
  content: "原始正文",
  title: "预约需求",
  questions: [],
};
const reviewed = {
  id: "review-one",
  status: "ready",
  source_version: 3,
  source_content: "原始正文",
  candidate: "修订正文",
  events: [],
  error: "",
  report: {
    verified: true,
    partial: false,
    summary: "核对了取消规则。",
    suggested_fixes: 1,
    questions: [],
    blocks: [{ id: "b1", title: "取消预约" }],
    changes: [{ block_id: "b1", old_text: "仍占用时间", new_text: "释放时段" }],
    findings: [],
  },
};
function mount(handler, extra = {}) {
  const root = document.createElement("div");
  document.body.append(root);
  const props = vue.reactive({
    projectId: "project-one",
    item: { ...item },
    configured: true,
    locked: false,
    ...extra,
  });
  const View = component(
    "../src/components/DocumentReview.vue",
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
function button(root, text) {
  return [...root.querySelectorAll("button")].find((b) =>
    b.textContent.includes(text),
  );
}

test("review shows source diff and only changes the document after explicit apply", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes.push(JSON.parse(init.body));
      return {
        item: { ...item, content: "修订正文", version: 4 },
        review: { ...reviewed, status: "applied" },
      };
    }
    return reviewed;
  });
  try {
    await settle();
    assert.equal(view.changed.length, 0);
    assert.match(view.root.textContent, /正文尚未改变/);
    assert.match(view.root.textContent, /查看修改差异/);
    assert.match(view.root.textContent, /仍占用时间/);
    button(view.root, "应用修改").click();
    await settle();
    assert.equal(writes.length, 1);
    assert.equal(writes[0].expected_version, 3);
    assert.equal(view.changed[0].content, "修订正文");
    view.props.item = view.changed[0];
    await settle();
    assert.match(view.root.textContent, /优化修改已应用/);
    view.props.item = {
      ...view.changed[0],
      content: "后来恢复的正文",
      version: 5,
    };
    await settle();
    assert.match(view.root.textContent, /历史检查记录，正文已另行修改/);
  } finally {
    view.close();
  }
});

test("network uncertainty retries the identical action and retains the review", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (!init?.method) return reviewed;
    writes.push(init.body);
    if (writes.length === 1) throw new client.ApiError("连接中断", 0);
    return {
      item: { ...item, version: 4 },
      review: { ...reviewed, status: "applied" },
    };
  });
  try {
    await settle();
    button(view.root, "应用修改").click();
    await settle();
    assert.ok(button(view.root, "应用修改").disabled);
    assert.match(view.root.textContent, /原请求重试/);
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

test("stale or published documents never enable applying a review", async () => {
  const view = mount(async () => reviewed, { item: { ...item, version: 4 } });
  try {
    await settle();
    assert.ok(button(view.root, "应用修改").disabled);
    assert.match(view.root.textContent, /正文已变化/);
    view.props.item = { ...item, status: "approved" };
    await settle();
    assert.ok(button(view.root, "应用修改").disabled);
    assert.match(view.root.textContent, /正式版本保持锁定/);
  } finally {
    view.close();
  }
});

test("restoring history requires an explicit selection and creates a version-checked action", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes.push({ path, body: JSON.parse(init.body) });
      return { item: { ...item, content: "旧稿", version: 4 } };
    }
    if (path.endsWith("/history"))
      return [
        {
          id: "old-one",
          title: "预约需求",
          content: "旧稿",
          version: 2,
          created_at: "2026-10-27T00:00:00Z",
        },
      ];
    return null;
  });
  try {
    await settle();
    button(view.root, "历史内容").click();
    await settle();
    button(view.root, "恢复此稿").click();
    await settle();
    assert.equal(writes.length, 0);
    button(view.root, "确认恢复内容").click();
    await settle();
    assert.equal(writes.length, 1);
    assert.match(writes[0].path, /history\/old-one\/restore/);
    assert.equal(writes[0].body.expected_version, 3);
    assert.equal(view.changed[0].content, "旧稿");
    view.props.item = view.changed[0];
    await settle();
    assert.match(view.root.textContent, /历史内容已恢复为一次新修订/);
  } finally {
    view.close();
  }
});

test("late review responses cannot populate a newly opened document", async () => {
  let resolve;
  const late = new Promise((done) => {
    resolve = done;
  });
  const view = mount(async (path) => (path.includes("item-one") ? late : null));
  try {
    await settle();
    view.props.item = { ...item, id: "item-two" };
    await settle();
    resolve(reviewed);
    await settle();
    assert.equal(button(view.root, "应用修改"), undefined);
  } finally {
    view.close();
  }
});

test("incomplete review exposes its cause and never presents zero findings as a clean review", async () => {
  const view = mount(async () => ({
    ...reviewed,
    report: {
      partial: true,
      verified: true,
      suggested_fixes: 0,
      findings: [],
      changes: [],
      stop_reasons: ["原文依据未匹配，已停止重复尝试。"],
      summary: "本次检查未能形成有效结论，不能据此判断草案没有问题。",
    },
  }));
  try {
    await settle();
    assert.match(view.root.textContent, /本次检查未完成/);
    assert.match(view.root.textContent, /已停止重复尝试/);
    assert.ok(button(view.root, "应用修改").disabled);
  } finally {
    view.close();
  }
});
