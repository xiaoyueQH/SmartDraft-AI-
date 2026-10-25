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
function mount(handler, role = "user", props = {}) {
  const root = document.createElement("div");
  document.body.append(root);
  const state = vue.reactive({ user: { id: "alice", role } });
  const View = component(
    "../src/views/ProjectsView.vue",
    { auth: { request: handler }, authState: state },
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
      if (path.endsWith("/activity")) return [];
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
      if (path.endsWith("/activity")) return [];
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
    assert.match(view.root.textContent, /Agent 尚未配置模型/);
    assert.ok(button(view.root, "发送给 Agent").disabled);
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
    assert.ok(button(view.root, "整理需求草案").disabled);
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
