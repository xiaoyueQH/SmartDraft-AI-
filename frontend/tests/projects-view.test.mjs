import assert from "node:assert/strict";
import { test } from "node:test";
import * as projects from "../src/api/projects.ts";
import * as client from "../src/auth/client.ts";
import { component, dom, settle, vue } from "./sfc-support.mjs";
import { marked } from "marked";
import createDOMPurify from "dompurify";

const project = {
  id: "project-one",
  owner_id: "alice",
  title: "仓库管理",
  brief: "入库出库",
  stack: "Python",
  revision: 1,
  phase: "project",
  discovery: {},
  version: 1,
  created_at: "2026-10-25T00:00:00Z",
  deleted_at: null,
};
const doc = {
  id: "doc-one",
  project_id: project.id,
  kind: "change",
  title: "增加预警",
  content: "完整需求书",
  questions: [],
  status: "submitted",
  base_revision: 1,
  customer_confirmed: false,
  staff_confirmed: true,
  version: 2,
  created_by: "alice",
  resolution: "",
  acceptance_note: "",
};
const snapshot = () => ({
  project,
  baseline: null,
  items: [doc],
  messages: [],
  runs: [],
  has_more_items: false,
});
function button(root, text) {
  return [...root.querySelectorAll("button")].find((b) =>
    b.textContent.includes(text),
  );
}
function mount(handler, role = "user", props = {}, stream = undefined) {
  const root = document.createElement("div");
  document.body.append(root);
  const state = vue.reactive({ user: { id: "alice", role } });
  const View = component(
    "../src/views/ProjectsView.vue",
    { auth: { request: handler, stream }, authState: state },
    {
      "../api/projects": projects,
      "../auth/client": client,
      marked: { marked },
      dompurify: { default: createDOMPurify(dom.window) },
      "vue-router": { onBeforeRouteLeave() {}, onBeforeRouteUpdate() {} },
      mermaid: {
        default: {
          initialize() {},
          async render() {
            return { svg: "<svg></svg>" };
          },
        },
      },
    },
  );
  const viewProps = vue.reactive(props);
  const app = vue.createApp({ render: () => vue.h(View, viewProps) });
  app.mount(root);
  return {
    root,
    props: viewProps,
    close() {
      app.unmount();
      root.remove();
    },
  };
}
const request = async (path) =>
  path.includes("/agent")
    ? { configured: false }
    : path.includes("?offset=")
      ? { items: [project], has_more: false }
      : snapshot();

test("customer can request a human while AI is running and continue sending while pending", async () => {
  const active = {
    id: "active",
    status: "running",
    prompt: "原问题",
    answer: "",
    events: [],
    created_at: "2026-10-25T00:00:00Z",
  };
  let control = {
    mode: "ai",
    version: 1,
    support_id: null,
    support_name: "",
    last_sequence: 0,
    facts_cursor: 0,
    extraction_pending: false,
    extraction_error: "",
  };
  let messages = [],
    sent = 0;
  const view = mount(async (path, init) => {
    if (path.endsWith("/conversation/actions")) {
      control = { ...control, mode: "pending", version: 2 };
      return { conversation: control };
    }
    if (path.endsWith("/messages") && init?.method === "POST") {
      sent++;
      messages = [
        {
          id: "human-new",
          role: "user",
          content: "人工请帮我处理",
          sender_type: "customer",
          sender_id: "alice",
        },
      ];
      control = { ...control, last_sequence: 1 };
      return { delivery: "chat", status: "delivered", conversation: control };
    }
    if (path.endsWith("/conversation"))
      return {
        conversation: control,
        messages,
        run: active,
        project_version: 1,
        discovery: {},
        memory_pending: false,
      };
    if (path.endsWith("/agent")) return { configured: false };
    if (path.includes("?offset=")) return { items: [project] };
    return {
      ...snapshot(),
      project: { ...project, conversation_mode: "ai", conversation_version: 1 },
      runs: [active],
    };
  });
  try {
    await settle();
    const contact = button(view.root, "联系人工");
    assert.equal(contact.disabled, false);
    contact.click();
    await settle();
    assert.match(view.root.textContent, /等待本轮AI完成/);
    const input = view.root.querySelector("#project-prompt");
    input.value = "人工请帮我处理";
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    await settle();
    assert.equal(button(view.root, "发送").disabled, false);
    button(view.root, "发送").click();
    await settle();
    assert.equal(sent, 1);
    assert.match(view.root.textContent, /人工请帮我处理/);
  } finally {
    view.close();
  }
});

test("assigned admin sees real message authors and can reply without a configured model", async () => {
  let sent = 0;
  const control = {
    mode: "human",
    version: 2,
    support_id: "alice",
    support_name: "客服一",
    last_sequence: 2,
    facts_cursor: 2,
    extraction_pending: false,
    extraction_error: "",
  };
  const messages = [
    {
      id: "customer-msg",
      role: "user",
      content: "我要查询订单",
      sender_id: "customer",
      sender_type: "customer",
      sender_name: "小王",
    },
  ];
  const view = mount(async (path, init) => {
    if (path.endsWith("/messages") && init?.method === "POST") {
      sent++;
      return { delivery: "chat", conversation: control };
    }
    if (path.endsWith("/conversation"))
      return {
        conversation: control,
        messages,
        run: null,
        project_version: 1,
        discovery: {},
        memory_pending: false,
      };
    if (path.endsWith("/agent")) return { configured: false };
    if (path.includes("?offset=")) return { items: [project] };
    return {
      ...snapshot(),
      project: {
        ...project,
        owner_id: "customer",
        conversation_mode: "human",
        conversation_version: 2,
        support_id: "alice",
      },
      messages,
    };
  }, "admin");
  try {
    await settle();
    assert.match(view.root.textContent, /客户 小王/);
    const input = view.root.querySelector("#project-prompt");
    input.value = "您好，我来帮您查一下。";
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    await settle();
    assert.equal(button(view.root, "发送").disabled, false);
    button(view.root, "发送").click();
    await settle();
    assert.equal(sent, 1);
    assert.ok(button(view.root, "交还AI"));
  } finally {
    view.close();
  }
});

test("document summaries fetch the complete authorized document only when opened", async () => {
  let reads = 0;
  const view = mount(async (path) => {
    if (path.endsWith(`/items/${doc.id}`)) {
      reads++;
      return { ...doc, content: "这里才是按需读取的完整正文" };
    }
    if (path.includes("/activity") || path.endsWith("/history")) return [];
    if (path.includes("/reviews")) return null;
    if (path.endsWith("/agent")) return { configured: true };
    if (path.includes("?offset=")) return { items: [project] };
    return {
      ...snapshot(),
      items: [{ ...doc, content: "短摘要", content_loaded: false }],
    };
  });
  try {
    await settle();
    assert.equal(reads, 0);
    button(view.root, "需求与变更").click();
    await settle();
    button(view.root, "增加预警").click();
    await settle();
    assert.equal(reads, 1);
    assert.match(
      view.root.querySelector("dialog[open]").textContent,
      /这里才是按需读取的完整正文/,
    );
  } finally {
    view.close();
  }
});

test("an older execution-details response cannot replace a newer response", async () => {
  const responses = [];
  const rid = "run-one";
  const row = {
    id: rid,
    status: "completed",
    prompt: "",
    answer: "",
    events: [],
    created_at: "2026-10-25T00:00:00Z",
    cache_version: 1,
    details_loaded: false,
  };
  const view = mount(async (path) => {
    if (path.endsWith(`/runs/${rid}`))
      return new Promise((resolve) => responses.push(resolve));
    if (path.endsWith("/agent")) return { configured: true };
    if (path.includes("?offset=")) return { items: [project] };
    return {
      ...snapshot(),
      runs: [row],
      messages: [
        {
          id: "message",
          role: "assistant",
          content: "已完成沟通",
          run_id: rid,
        },
      ],
    };
  });
  try {
    await settle();
    const trigger = button(view.root, "查看处理过程");
    trigger.click();
    trigger.click();
    await settle();
    responses[1]({
      ...row,
      details_loaded: true,
      cache_version: 3,
      events: [{ sequence: 1, type: "node", summary: "较新的处理记录" }],
    });
    await settle();
    responses[0]({
      ...row,
      details_loaded: true,
      cache_version: 2,
      events: [{ sequence: 1, type: "node", summary: "过期的处理记录" }],
    });
    await settle();
    assert.ok(!view.root.textContent.includes("过期的处理记录"));
    assert.ok(!button(view.root, "查看处理过程"));
  } finally {
    view.close();
  }
});

test("an older stream frame cannot replace newly loaded execution details", async () => {
  let deliver, end;
  const active = {
    id: "race-run",
    status: "running",
    prompt: "预约",
    answer: "",
    events: [],
    created_at: "2026-10-25T00:00:00Z",
    cache_version: 1,
    details_loaded: false,
  };
  const view = mount(
    async (path) => {
      if (path.endsWith(`/runs/${active.id}`))
        return {
          ...active,
          status: "completed",
          cache_version: 3,
          details_loaded: true,
        };
      if (path.endsWith("/agent")) return { configured: true };
      if (path.includes("?offset=")) return { items: [project] };
      return {
        ...snapshot(),
        runs: [active],
        messages: [
          {
            id: "a",
            role: "assistant",
            content: "沟通结果",
            run_id: active.id,
          },
        ],
      };
    },
    "user",
    {},
    async (_path, receive) => {
      deliver = receive;
      await new Promise((resolve) => {
        end = resolve;
      });
    },
  );
  try {
    await settle();
    button(view.root, "查看处理过程").click();
    await settle();
    deliver("run", { ...active, cache_version: 2 });
    await settle();
    assert.equal(view.root.querySelector(".chat-stream-text"), null);
    assert.equal(button(view.root, "查看处理过程"), undefined);
    assert.ok(!view.root.textContent.includes("正在回复"));
  } finally {
    view.close();
    if (end) end();
  }
});

test("initial requirement management opens the current draft instead of creating another", async () => {
  const initial = { ...project, revision: 0, phase: "discovery" };
  const current = {
    ...doc,
    kind: "specification",
    status: "withdrawn",
    published_revision: null,
    base_revision: 0,
  };
  let writes = 0;
  const view = mount(
    async (path, init) => {
      if (init?.method === "POST") writes++;
      if (path.includes("/activity") || path.endsWith("/history")) return [];
      if (path.includes("/reviews/latest")) return null;
      if (path.endsWith("/agent")) return { configured: true };
      if (path.includes("?offset=")) return { items: [initial] };
      return { ...snapshot(), project: initial, items: [current] };
    },
    "admin",
    { admin: true, projectId: initial.id },
  );
  try {
    await settle();
    button(view.root, "需求草案").click();
    await settle();
    assert.equal(button(view.root, "新建需求 / 变更"), undefined);
    button(view.root, "查看最新需求").click();
    await settle();
    assert.match(view.root.textContent, /可编辑当前需求草案后重新提交/);
    assert.ok(button(view.root, "编辑草稿"));
    assert.equal(writes, 0);
  } finally {
    view.close();
  }
});

test("record selector uses the latest project summary without another list request", async () => {
  const latest = { ...project, title: "设备借用管理", version: 3 };
  let listReads = 0;
  const view = mount(async (path) => {
    if (path.endsWith("/agent")) return { configured: true };
    if (path.includes("?offset=")) {
      listReads++;
      return { items: [project] };
    }
    return { ...snapshot(), project: latest };
  });
  try {
    await settle();
    const selector = view.root.querySelector("#project-select");
    assert.match(selector.selectedOptions[0].textContent, /设备借用管理/);
    assert.equal(selector.value, latest.id);
    assert.equal(listReads, 1);
  } finally {
    view.close();
  }
});

test("drafts never present the initial baseline or edit counter as a released version", () => {
  assert.equal(
    projects.itemVersionLabel({ ...doc, base_revision: 0, version: 9 }),
    "初始需求 · 尚未发布",
  );
  assert.equal(projects.itemVersionLabel(doc), "基于正式 V1");
  assert.equal(
    projects.itemVersionLabel({ ...doc, published_revision: 2, version: 18 }),
    "正式 V2",
  );
  assert.equal(projects.itemStateLabel(doc), "待双方评审");
  assert.equal(projects.itemStateLabel({ ...doc, kind: "bug" }), "待受理");
});

test("uncertain submission retries the identical request and exposes the next review step in place", async () => {
  let current = {
    ...doc,
    kind: "specification",
    status: "draft",
    base_revision: 0,
    staff_confirmed: false,
  };
  const writes = [];
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes.push(init.body);
      if (writes.length === 1) throw new client.ApiError("网络中断", 0);
      current = { ...current, status: "submitted", version: 3 };
      return { item: current, revision: 0 };
    }
    if (path.includes("/activity")) return [];
    if (path.endsWith("/agent")) return { configured: true };
    if (path.includes("?offset=")) return { items: [project] };
    return { ...snapshot(), items: [current] };
  });
  try {
    await settle();
    button(view.root, "需求与变更").click();
    await settle();
    button(view.root, "增加预警").click();
    await settle();
    button(view.root, "提交评审").click();
    await settle();
    assert.ok(button(view.root, "提交评审").disabled);
    const dialog = view.root.querySelector(".project-document-dialog");
    button(dialog, "原请求重试").click();
    await settle();
    assert.equal(writes[0], writes[1]);
    assert.ok(dialog.open);
    assert.equal(button(dialog, "提交评审"), undefined);
    assert.ok(button(dialog, "客户确认本版内容"));
    assert.match(dialog.textContent, /等待客户与负责人确认/);
    assert.doesNotMatch(dialog.textContent, /V0|结果待确认/);
  } finally {
    view.close();
  }
});

test("last party confirmation publishes V1 and replaces approval controls without closing", async () => {
  let current = {
    ...doc,
    kind: "specification",
    base_revision: 0,
    customer_confirmed: true,
    staff_confirmed: false,
  };
  let writes = 0;
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes++;
      assert.equal(JSON.parse(init.body).expected_version, 2);
      current = {
        ...current,
        staff_confirmed: true,
        status: "approved",
        published_revision: 1,
        version: 3,
      };
      return { item: current, revision: 1 };
    }
    if (path.includes("/activity")) return [];
    if (path.endsWith("/agent")) return { configured: true };
    if (path.includes("?offset=")) return { items: [project] };
    return { ...snapshot(), items: [current] };
  }, "admin");
  try {
    await settle();
    button(view.root, "需求与变更").click();
    await settle();
    button(view.root, "增加预警").click();
    await settle();
    button(view.root, "负责人确认本版内容").click();
    await settle();
    assert.equal(writes, 0, "the review action must wait for confirmation");
    assert.match(view.root.textContent, /操作确认/);
    button(view.root, "取消操作").click();
    await settle();
    assert.equal(writes, 0);
    button(view.root, "负责人确认本版内容").click();
    await settle();
    button(view.root, "确定执行").click();
    await settle();
    assert.equal(writes, 1);
    const dialog = view.root.querySelector(".project-document-dialog");
    assert.ok(dialog.open);
    assert.match(dialog.textContent, /已发布正式 V1/);
    assert.equal(button(dialog, "负责人确认本版内容"), undefined);
    assert.equal(button(dialog, "编辑草稿"), undefined);
  } finally {
    view.close();
  }
});

test("switching projects during a saved action refresh discards the old record history response", async () => {
  const delayed = deferred();
  let saved = false;
  const paths = [];
  const first = { ...doc, project_id: "first", status: "draft" };
  const view = mount(
    async (path, init) => {
      paths.push(path);
      if (init?.method === "POST") {
        saved = true;
        return { item: { ...first, status: "submitted", version: 3 } };
      }
      if (path.includes("/activity")) return [];
      if (path.endsWith("/agent")) return { configured: true };
      if (path.includes("?offset=")) return { items: [] };
      if (path.includes("/items/")) return first;
      if (path === "/api/projects/first" && saved) return delayed.promise;
      const id = path.endsWith("/second") ? "second" : "first";
      return {
        ...snapshot(),
        project: { ...project, id, title: `${id}项目` },
        items: [first],
      };
    },
    "admin",
    { projectId: "first", initialTab: "documents", initialItemId: first.id },
  );
  try {
    await settle();
    button(
      view.root.querySelector(".project-document-dialog"),
      "提交评审",
    ).click();
    await settle();
    view.props.initialItemId = undefined;
    view.props.projectId = "second";
    await settle();
    delayed.resolve(snapshot());
    await settle();
    assert.equal(
      view.root.querySelector(".project-summary h2").textContent,
      "second项目",
    );
    assert.ok(
      !paths.some((path) => path.includes("/second/activity?item_id=doc-one")),
    );
    assert.doesNotMatch(view.root.textContent, /暂未同步/);
  } finally {
    view.close();
  }
});

test("admin detail route opens the requested project and exact business record", async () => {
  const requested = {
    ...project,
    id: "requested-project",
    title: "指定客户项目",
  };
  const target = {
    ...doc,
    id: "requested-item",
    project_id: requested.id,
    staff_confirmed: false,
  };
  const view = mount(
    async (path) => {
      if (path.includes("?offset=")) return { items: [project] };
      if (path.endsWith("/agent")) return { configured: true };
      if (path.endsWith(`/items/${target.id}`)) return target;
      if (path.includes("/activity")) return [];
      return { ...snapshot(), project: requested, items: [target] };
    },
    "admin",
    {
      projectId: requested.id,
      initialTab: "documents",
      initialItemId: target.id,
    },
  );
  try {
    await settle();
    assert.equal(
      view.root.querySelector(".project-summary h2").textContent,
      requested.title,
    );
    assert.ok(view.root.querySelector(".project-document-dialog").open);
    assert.ok(button(view.root, "负责人确认本版内容"));
    assert.equal(view.root.querySelector("#project-select"), null);
  } finally {
    view.close();
  }
});

test("changing an admin detail route replaces the project and its open record", async () => {
  const view = mount(
    async (path) => {
      if (path.includes("?offset=")) return { items: [] };
      if (path.endsWith("/agent")) return { configured: true };
      if (path.includes("/activity")) return [];
      const id = path.includes("/second") ? "second" : "first";
      if (path.includes("/items/"))
        return { ...doc, id: `${id}-item`, title: `${id}需求`, project_id: id };
      return { ...snapshot(), project: { ...project, id, title: `${id}项目` } };
    },
    "admin",
    {
      projectId: "first",
      initialTab: "documents",
      initialItemId: "first-item",
    },
  );
  try {
    await settle();
    assert.equal(
      view.root.querySelector(".project-summary h2").textContent,
      "first项目",
    );
    view.props.projectId = "second";
    view.props.initialItemId = "second-item";
    await settle();
    assert.equal(
      view.root.querySelector(".project-summary h2").textContent,
      "second项目",
    );
    assert.equal(
      view.root.querySelector(".project-document-dialog h2").textContent,
      "second需求",
    );
    assert.ok(view.root.querySelector(".project-document-dialog").open);
  } finally {
    view.close();
  }
});

test("workspace exposes project workflows and clearly marks unconfigured Agent", async () => {
  const view = mount(request);
  try {
    await settle();
    for (const text of [
      "需求沟通",
      "需求与变更",
      "售后工单",
      "开发任务",
      "交付验收",
    ])
      assert.ok(button(view.root, text));
    assert.match(view.root.textContent, /业务顾问暂时不可用/);
    assert.ok(button(view.root, "发送").disabled);
    button(view.root, "开发任务").click();
    await settle();
    assert.equal(button(view.root, "新建任务"), undefined);
  } finally {
    view.close();
  }
});

test("customer sees exact confirmation status and a preserved published document", async () => {
  const view = mount(request);
  try {
    await settle();
    button(view.root, "需求与变更").click();
    await settle();
    button(view.root, "增加预警").click();
    await settle();
    assert.ok(view.root.querySelector("dialog.project-document-dialog").open);
    assert.match(view.root.textContent, /负责人：已确认/);
    assert.ok(button(view.root, "客户确认本版内容"));
    assert.match(
      view.root.querySelector(".requirement-document").textContent,
      /完整需求书/,
    );
  } finally {
    view.close();
  }
});

test("starting a conversation needs no project form and retries the exact original request", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes.push({ path, body: init.body });
      if (writes.length === 1) throw new client.ApiError("网络中断", 0);
      return { ...project, id: "created", phase: "discovery" };
    }
    return request(path);
  });
  try {
    await settle();
    button(view.root, "开始需求沟通").click();
    await settle();
    assert.equal(view.root.querySelector("dialog.project-dialog").open, false);
    assert.equal(writes[0].path, "/api/consultations");
    assert.deepEqual(Object.keys(JSON.parse(writes[0].body)), ["request_id"]);
    assert.ok(button(view.root, "开始需求沟通").disabled);
    button(view.root, "原请求重试").click();
    await settle();
    assert.equal(writes.length, 2);
    assert.deepEqual(writes[0], writes[1]);
  } finally {
    view.close();
  }
});

test("ticket result and customer acceptance note are visible together", async () => {
  const ticket = {
    ...doc,
    kind: "bug",
    title: "出库错误",
    status: "closed",
    resolution: "已提交修复与验证记录",
    acceptance_note: "客户复测通过",
  };
  const view = mount(async (path) =>
    path.includes("/agent")
      ? { configured: true }
      : path.includes("?offset=")
        ? { items: [project] }
        : { ...snapshot(), items: [ticket] },
  );
  try {
    await settle();
    button(view.root, "售后工单").click();
    await settle();
    button(view.root, "出库错误").click();
    await settle();
    assert.match(view.root.textContent, /已提交修复与验证记录/);
    assert.match(view.root.textContent, /客户复测通过/);
    assert.equal(button(view.root, "开始处理"), undefined);
  } finally {
    view.close();
  }
});

test("requirement preview removes executable HTML and remote image sources", async () => {
  const unsafe = {
    ...doc,
    content:
      '# 正常需求\n<script>alert(1)</script><img src="https://invalid.example/pixel"><svg><image href="https://invalid.example/pixel" /></svg>\n**保留业务说明**',
  };
  const view = mount(async (path) =>
    path.includes("/agent")
      ? { configured: true }
      : path.includes("?offset=")
        ? { items: [project] }
        : { ...snapshot(), items: [unsafe] },
  );
  try {
    await settle();
    button(view.root, "需求与变更").click();
    await settle();
    button(view.root, "增加预警").click();
    await settle();
    const preview = view.root.querySelector(".requirement-document");
    assert.equal(preview.querySelector("script, img, svg, image"), null);
    assert.match(preview.textContent, /保留业务说明/);
    assert.equal(preview.querySelector("h1").textContent, "正常需求");
  } finally {
    view.close();
  }
});

test("discovery starts with business questions and hides project delivery modules", async () => {
  const consultation = {
    ...project,
    revision: 0,
    phase: "discovery",
    stack: "待确认",
    discovery: {
      business_background: "维修门店",
      problems: ["电话预约容易漏单"],
      open_questions: ["谁负责排班？"],
    },
  };
  const view = mount(async (path) =>
    path.includes("/agent")
      ? { configured: true }
      : path.includes("?offset=")
        ? { items: [consultation] }
        : { ...snapshot(), project: consultation, items: [] },
  );
  try {
    await settle();
    assert.match(view.root.textContent, /您目前主要做什么业务/);
    assert.match(view.root.textContent, /维修门店/);
    assert.match(view.root.textContent, /电话预约容易漏单/);
    assert.ok(button(view.root, "需求草案"));
    assert.equal(button(view.root, "开发任务"), undefined);
    assert.equal(button(view.root, "售后工单"), undefined);
    assert.equal(
      view.root.querySelector('input[placeholder="请填写项目背景"]'),
      null,
    );
    assert.equal(button(view.root, "整理需求草案"), undefined);
    assert.match(view.root.textContent, /沟通内容会先保存；需要时直接告诉顾问/);
  } finally {
    view.close();
  }
});

test("administrator selects only a customer when starting requirement discovery", async () => {
  const view = mount(
    async (path) =>
      path.startsWith("/api/admin/users")
        ? [
            {
              id: "customer-1",
              username: "client",
              display_name: "客户",
              role: "user",
              is_active: true,
            },
          ]
        : request(path),
    "admin",
  );
  try {
    await settle();
    button(view.root, "开始需求沟通").click();
    await settle();
    const dialog = view.root.querySelector("dialog.project-dialog");
    assert.ok(dialog.open);
    assert.equal(dialog.querySelectorAll("select").length, 1);
    assert.equal(dialog.querySelectorAll("input,textarea").length, 0);
    assert.match(dialog.textContent, /无需预先填写项目资料/);
  } finally {
    view.close();
  }
});

const emptyConsultation = {
  ...project,
  id: "new-consultation",
  title: "需求沟通",
  brief: "",
  stack: "待确认",
  revision: 0,
  phase: "discovery",
  discovery: {},
};

test("research preference needs no per-query approval or generation confirmation", async () => {
  const current = {
    ...emptyConsultation,
    consultation: {
      status: "waiting_user",
      draft_consent: false,
      gaps: [],
      conflicts: [],
      suggestions: [],
      references: [],
    },
  };
  const view = mount(async (path) => {
    if (path === "/api/agent/tools") return { web_search: { enabled: true } };
    if (path.endsWith("/agent"))
      return { configured: true, tools: { web_search: { enabled: true } } };
    if (path.includes("?offset=")) return { items: [current] };
    return { ...snapshot(), project: current, items: [] };
  });
  try {
    await settle();
    assert.equal(
      view.root.querySelector(".consultation-search-approval"),
      null,
    );
    assert.equal(button(view.root, "仅允许本次查询"), undefined);
    assert.equal(button(view.root, "整理需求草案"), undefined);
    assert.ok(view.root.querySelector(".web-search-option input"));
  } finally {
    view.close();
  }
});
const emptySnapshot = () => ({
  ...snapshot(),
  project: emptyConsultation,
  items: [],
});
function deferred() {
  let resolve;
  const promise = new Promise((finish) => {
    resolve = finish;
  });
  return { promise, resolve };
}

test("creation keeps the workspace mounted and blocks extra clicks through every read", async () => {
  const list = deferred(),
    details = deferred(),
    status = deferred();
  let writes = 0;
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes++;
      return emptyConsultation;
    }
    if (path.includes("?offset="))
      return writes ? list.promise : { items: [project] };
    if (path === `/api/projects/${emptyConsultation.id}/agent`)
      return status.promise;
    if (path === `/api/projects/${emptyConsultation.id}`)
      return details.promise;
    return request(path);
  });
  try {
    await settle();
    const chat = view.root.querySelector(".project-chat"),
      input = view.root.querySelector("#project-prompt");
    const start = button(view.root, "开始需求沟通");
    start.click();
    await settle();
    assert.ok(start.disabled);
    start.click();
    start.click();
    await settle();
    assert.equal(writes, 1);
    assert.equal(view.root.querySelector(".project-chat"), chat);
    assert.equal(view.root.querySelector("#project-prompt"), input);
    assert.ok(chat.isConnected);
    assert.equal(view.root.querySelector(".project-empty"), null);
    list.resolve({ items: [emptyConsultation, project], has_more: false });
    await settle();
    assert.ok(start.disabled);
    details.resolve(emptySnapshot());
    await settle();
    assert.equal(
      view.root.querySelector(".project-summary h2").textContent,
      project.title,
    );
    assert.ok(start.disabled);
    assert.equal(view.root.querySelector(".project-chat"), chat);
    status.resolve({ configured: true });
    await settle();
    assert.equal(
      view.root.querySelector(".project-summary h2").textContent,
      "需求沟通",
    );
    assert.equal(view.root.querySelector(".project-chat"), chat);
    assert.ok(!start.disabled);
    start.click();
    await settle();
    assert.equal(writes, 1);
  } finally {
    list.resolve({ items: [] });
    details.resolve(emptySnapshot());
    status.resolve({ configured: true });
    view.close();
  }
});

test("an unused consultation is reused without clearing the typed message or reloading", async () => {
  let writes = 0,
    reads = 0;
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes++;
      return emptyConsultation;
    }
    reads++;
    return path.includes("/agent")
      ? { configured: true }
      : path.includes("?offset=")
        ? { items: [emptyConsultation] }
        : emptySnapshot();
  });
  try {
    await settle();
    const before = reads;
    const input = view.root.querySelector("#project-prompt");
    input.value = "还没发出的业务情况";
    input.dispatchEvent(new dom.window.Event("input"));
    button(view.root, "开始需求沟通").click();
    await settle();
    button(view.root, "开始需求沟通").click();
    await settle();
    assert.equal(writes, 0);
    assert.equal(reads, before);
    assert.equal(view.root.querySelector("#project-prompt"), input);
    assert.equal(input.value, "还没发出的业务情况");
    assert.equal(document.activeElement, input);
  } finally {
    view.close();
  }
});

test("failed record switching retains the old workspace, selection and unsent input", async () => {
  const view = mount(async (path) => {
    if (path.includes("new-consultation"))
      throw new client.ApiError("读取失败", 503);
    if (path.includes("?offset="))
      return { items: [project, emptyConsultation] };
    return request(path);
  });
  try {
    await settle();
    const chat = view.root.querySelector(".project-chat");
    const input = view.root.querySelector("#project-prompt");
    input.value = "保留输入";
    input.dispatchEvent(new dom.window.Event("input"));
    const select = view.root.querySelector("#project-select");
    select.value = emptyConsultation.id;
    select.dispatchEvent(new dom.window.Event("change"));
    await settle();
    assert.equal(view.root.querySelector(".project-chat"), chat);
    assert.equal(select.value, project.id);
    assert.equal(input.value, "保留输入");
    assert.match(view.root.textContent, /读取失败/);
  } finally {
    view.close();
  }
});

test("a created consultation with a failed read is reopened without another creation", async () => {
  let writes = 0,
    failRead = true;
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      writes++;
      return emptyConsultation;
    }
    if (path.includes("?offset="))
      return { items: writes ? [emptyConsultation, project] : [project] };
    if (path.includes(emptyConsultation.id)) {
      if (failRead) throw new client.ApiError("临时读取失败", 503);
      return path.endsWith("/agent") ? { configured: true } : emptySnapshot();
    }
    return request(path);
  });
  try {
    await settle();
    button(view.root, "开始需求沟通").click();
    await settle();
    assert.equal(writes, 1);
    assert.equal(
      view.root.querySelector(".project-summary h2").textContent,
      project.title,
    );
    failRead = false;
    button(view.root, "开始需求沟通").click();
    await settle();
    assert.equal(writes, 1);
    assert.equal(
      view.root.querySelector("#project-select").value,
      emptyConsultation.id,
    );
  } finally {
    view.close();
  }
});

test("deleting the selected consultation switches to the next record", async () => {
  let deleted = false;
  const initial = { ...project, phase: "discovery", revision: 0 };
  const next = { ...initial, id: "next-record", title: "客户管理沟通" };
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      deleted = true;
      return {
        items: [{ ...initial, version: 2, deleted_at: "2026-10-25T01:00:00Z" }],
      };
    }
    if (path.includes("?offset="))
      return { items: deleted ? [next] : [initial, next], has_more: false };
    if (path.endsWith("/agent")) return { configured: true };
    return {
      ...snapshot(),
      project: path.endsWith(next.id) ? next : initial,
      items: [],
    };
  });
  try {
    await settle();
    const input = view.root.querySelector("#project-prompt");
    input.value = "旧记录未发送内容";
    input.dispatchEvent(new dom.window.Event("input"));
    button(view.root, "管理记录").click();
    await settle();
    const manager = view.root.querySelector(".record-manager");
    button(manager.querySelector("tbody tr"), "删除").click();
    await settle();
    const ack = manager.querySelector(".record-ack input");
    ack.checked = true;
    ack.dispatchEvent(new dom.window.Event("change"));
    await settle();
    button(manager, "确认删除").click();
    await settle();
    assert.equal(view.root.querySelector("#project-select").value, next.id);
    assert.equal(
      view.root.querySelector(".project-summary h2").textContent,
      next.title,
    );
    assert.equal(view.root.querySelector("#project-prompt").value, "");
  } finally {
    view.close();
  }
});

test("renaming the current record retains its unsent message", async () => {
  let current = { ...project, phase: "discovery", revision: 0 };
  const view = mount(async (path, init) => {
    if (init?.method === "POST") {
      const command = JSON.parse(init.body);
      current = { ...current, title: command.title, version: 2 };
      return { items: [current] };
    }
    if (path.includes("?offset=")) return { items: [current], has_more: false };
    if (path.endsWith("/agent")) return { configured: true };
    return { ...snapshot(), project: current, items: [] };
  });
  try {
    await settle();
    const input = view.root.querySelector("#project-prompt");
    input.value = "尚未发送的业务说明";
    input.dispatchEvent(new dom.window.Event("input"));
    button(view.root, "管理记录").click();
    await settle();
    const manager = view.root.querySelector(".record-manager");
    button(manager, "重命名").click();
    await settle();
    const title = manager.querySelector('form input:not([type="checkbox"])');
    title.value = "仓库业务访谈";
    title.dispatchEvent(new dom.window.Event("input"));
    await settle();
    manager
      .querySelector("form")
      .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
    await settle();
    assert.equal(
      view.root.querySelector(".project-summary h2").textContent,
      "仓库业务访谈",
    );
    assert.equal(view.root.querySelector("#project-prompt"), input);
    assert.equal(input.value, "尚未发送的业务说明");
  } finally {
    view.close();
  }
});

test("workspace subscribes to the original run and replaces preview without another message POST", async () => {
  let deliver, end;
  let posts = 0,
    stored = false;
  const active = {
    id: "run-stream",
    prompt: "讨论预约",
    answer: "",
    status: "running",
    events: [],
    created_at: "2026-10-25T00:00:00Z",
  };
  const view = mount(
    async (path, init) => {
      if (init?.method === "POST") {
        posts++;
        return active;
      }
      if (path.endsWith("/agent")) return { configured: true };
      if (path.includes("?offset=")) return { items: [project] };
      return {
        ...snapshot(),
        runs: stored ? [{ ...active, status: "completed" }] : [],
        messages: stored
          ? [
              {
                id: "u",
                role: "user",
                run_id: active.id,
                content: active.prompt,
              },
              {
                id: "a",
                role: "assistant",
                run_id: active.id,
                content: "从预约登记开始。",
              },
            ]
          : [],
      };
    },
    "user",
    {},
    async (path, receive) => {
      assert.equal(
        path,
        `/api/projects/${project.id}/runs/${active.id}/stream`,
      );
      deliver = receive;
      await new Promise((resolve) => {
        end = resolve;
      });
    },
  );
  try {
    await settle();
    const input = view.root.querySelector("#project-prompt");
    input.value = active.prompt;
    input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    await settle();
    button(view.root, "发送").click();
    await settle();
    deliver("run", {
      ...active,
      preview: { kind: "reply", title: "", text: "从预约" },
    });
    await settle();
    assert.equal(
      view.root.querySelector(".chat-stream-text").textContent,
      "从预约",
    );
    stored = true;
    deliver("run", {
      ...active,
      status: "completed",
      answer: "从预约登记开始。",
      preview: null,
    });
    end();
    await settle();
    assert.equal(posts, 1);
    assert.equal(view.root.querySelector(".chat-stream-text"), null);
    assert.equal(
      view.root.querySelectorAll(".project-message.assistant").length,
      1,
    );
    assert.match(view.root.textContent, /从预约登记开始。/);
  } finally {
    if (end) end();
    view.close();
  }
});
