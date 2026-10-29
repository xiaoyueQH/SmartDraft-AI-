import assert from "node:assert/strict";
import { test } from "node:test";
import * as projects from "../src/api/projects.ts";
import * as client from "../src/auth/client.ts";
import { component, dom, settle, vue, vueRouter } from "./sfc-support.mjs";

function button(root, text) {
  return [...root.querySelectorAll("button")].find((node) =>
    node.textContent.includes(text),
  );
}
function mount(request, props = {}) {
  const root = document.createElement("div");
  document.body.append(root);
  const pushed = [];
  const View = component(
    "../src/views/AdminProjectsView.vue",
    { auth: { request } },
    {
      "../api/projects": projects,
      "../auth/client": client,
      "vue-router": {
        ...vueRouter,
        useRouter: () => ({ push: (target) => pushed.push(target) }),
        onBeforeRouteLeave() {},
      },
    },
  );
  const state = vue.reactive({ section: "overview", ...props });
  const app = vue.createApp({ render: () => vue.h(View, state) });
  app.mount(root);
  return {
    root,
    pushed,
    state,
    close() {
      app.unmount();
      root.remove();
    },
  };
}
const row = {
  id: "project-one",
  title: "预约管理",
  owner_id: "alice",
  customer_name: "张先生",
  customer_username: "alice",
  phase: "project",
  revision: 1,
  created_at: "2026-10-25T00:00:00Z",
};

const supportRow = {
  ...row,
  conversation_mode: "human",
  support_id: null,
  support_name: "",
  support_notice: "",
  human_only: false,
  reasons: ["waiting", "expiring"],
  failure_summary: "",
  deadline_at: "2026-10-30T08:03:00Z",
  wait_since: "2026-10-30T08:00:00Z",
};
const supportResult = (items = [supportRow], extra = {}) => ({
  items,
  counts: {
    total: 1,
    waiting: 1,
    expiring: 1,
    failed: 0,
    unanswered: 0,
    handover: 0,
    human_only: 0,
  },
  has_more: false,
  checked_at: "2026-10-30T08:02:30Z",
  ...extra,
});
const noRecords = { items: [], has_more: false };

test("support inbox displays reasons, assignment and next action without starting a model", async () => {
  const calls = [];
  const view = mount(async (path, init) => {
    calls.push({ path, init });
    return path.includes("/conversations/requests")
      ? supportResult()
      : path.endsWith("summary")
        ? {}
        : noRecords;
  });
  try {
    await settle();
    const panel = view.root.querySelector(".support-inbox");
    assert.match(panel.textContent, /等待接待/);
    assert.match(panel.textContent, /即将到期/);
    assert.match(panel.textContent, /尚未领取/);
    assert.match(panel.textContent, /接管会话后回复客户/);
    button(panel, "查看并处理").click();
    assert.equal(view.pushed[0], "/admin/projects/project-one");
    assert.ok(calls.every(({ init }) => init?.method !== "POST"));
    const request = calls.find(({ path }) =>
      path.includes("/conversations/requests"),
    );
    assert.equal(
      new URL(request.path, "http://localhost").searchParams.get("scope"),
      "all",
    );
  } finally {
    view.close();
  }
});

test("support search and reason are sent to server and failures use readable guidance", async () => {
  const calls = [];
  const view = mount(async (path) => {
    if (!path.includes("/conversations/requests"))
      return path.endsWith("summary") ? {} : noRecords;
    calls.push(path);
    const filtered =
      new URL(path, "http://localhost").searchParams.get("reason") === "failed";
    return supportResult(
      filtered
        ? [
            {
              ...supportRow,
              reasons: ["failed"],
              conversation_mode: "ai",
              deadline_at: null,
              failure_summary: "客户需重新登录",
              support_notice: "resume_required",
            },
          ]
        : [],
    );
  });
  try {
    await settle();
    const panel = view.root.querySelector(".support-inbox");
    const input = panel.querySelector("input");
    input.value = "张先生";
    input.dispatchEvent(new dom.window.Event("input"));
    const select = panel.querySelector("select");
    select.value = "failed";
    select.dispatchEvent(new dom.window.Event("change"));
    panel
      .querySelector("form")
      .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
    await settle();
    const params = new URL(calls.at(-1), "http://localhost").searchParams;
    assert.equal(params.get("reason"), "failed");
    assert.equal(params.get("q"), "张先生");
    assert.match(panel.textContent, /客户需重新登录/);
    assert.match(panel.textContent, /联系客户重新登录，或接管后人工协助/);
    assert.doesNotMatch(panel.textContent, /resume_required|private/);
  } finally {
    view.close();
  }
});

test("failed inbox reads show a retry instead of claiming the queue is empty", async () => {
  let failed = true;
  const view = mount(async (path) => {
    if (!path.includes("/conversations/requests"))
      return path.endsWith("summary") ? {} : noRecords;
    if (failed) throw new client.ApiError("接待服务暂时不可用", 503);
    return supportResult();
  });
  try {
    await settle();
    const panel = view.root.querySelector(".support-inbox");
    assert.match(panel.textContent, /接待服务暂时不可用/);
    assert.doesNotMatch(panel.textContent, /暂无待处理接待/);
    failed = false;
    button(panel, "重新读取接待").click();
    await settle();
    assert.match(panel.textContent, /张先生/);
    assert.doesNotMatch(panel.textContent, /接待服务暂时不可用/);
  } finally {
    view.close();
  }
});

test("support rows cannot open during a pending new filter", async () => {
  let resolve;
  const pending = new Promise((done) => {
    resolve = done;
  });
  const view = mount(async (path) => {
    if (!path.includes("/conversations/requests"))
      return path.endsWith("summary") ? {} : noRecords;
    return new URL(path, "http://localhost").searchParams.get("q")
      ? pending
      : supportResult();
  });
  try {
    await settle();
    const panel = view.root.querySelector(".support-inbox");
    const input = panel.querySelector("input");
    input.value = "next";
    input.dispatchEvent(new dom.window.Event("input"));
    panel
      .querySelector("form")
      .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
    await settle();
    assert.ok(button(panel, "查看并处理").disabled);
    button(panel, "查看并处理").click();
    assert.equal(view.pushed.length, 0);
    resolve(supportResult([]));
    await settle();
    assert.equal(button(panel, "查看并处理"), undefined);
  } finally {
    view.close();
  }
});

test("late support response cannot replace a newer search and stops on section change", async () => {
  let resolve;
  const delayed = new Promise((done) => {
    resolve = done;
  });
  const view = mount(async (path) => {
    if (!path.includes("/conversations/requests"))
      return path.endsWith("summary") ? {} : noRecords;
    return new URL(path, "http://localhost").searchParams.get("q")
      ? supportResult([{ ...supportRow, customer_name: "最新查询客户" }])
      : delayed;
  });
  try {
    await settle();
    const panel = view.root.querySelector(".support-inbox");
    const input = panel.querySelector("input");
    input.value = "最新";
    input.dispatchEvent(new dom.window.Event("input"));
    panel
      .querySelector("form")
      .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
    await settle();
    resolve(supportResult([{ ...supportRow, customer_name: "过期客户" }]));
    await settle();
    assert.match(panel.textContent, /最新查询客户/);
    assert.doesNotMatch(panel.textContent, /过期客户/);
    view.state.section = "projects";
    await settle();
    assert.equal(view.root.querySelector(".support-inbox"), null);
  } finally {
    view.close();
  }
});

test("admin landing page shows real business totals and customer records instead of a chat", async () => {
  const view = mount(async (path) =>
    path.includes("/conversations/requests")
      ? { items: [], counts: { total: 0 }, has_more: false }
      : path.endsWith("/summary")
        ? {
            consultations: 3,
            projects: 2,
            pending_reviews: 1,
            active_tasks: 4,
            active_deliveries: 0,
            open_service: 5,
          }
        : { items: [row], has_more: false },
  );
  try {
    await settle();
    assert.equal(view.root.querySelector(".project-chat"), null);
    assert.equal(
      view.root.querySelectorAll(".management-stats button").length,
      6,
    );
    assert.equal(
      view.root.querySelectorAll(".management-stats strong")[2].textContent,
      "1",
    );
    assert.match(view.root.textContent, /张先生/);
    button(view.root, "管理项目").click();
    assert.equal(view.pushed[0].path, "/admin/projects/project-one");
  } finally {
    view.close();
  }
});

test("review queue opens the exact project document and can change to the service queue", async () => {
  const calls = [];
  const view = mount(
    async (path) => {
      calls.push(path);
      return {
        items: [
          {
            ...row,
            id: "doc-one",
            project_id: row.id,
            project_title: row.title,
            kind: "change",
            status: "submitted",
            staff_confirmed: false,
            customer_confirmed: true,
          },
        ],
        has_more: false,
      };
    },
    { section: "reviews" },
  );
  try {
    await settle();
    assert.match(calls[0], /category=documents.*status=pending_review/);
    button(view.root, "查看并审核").click();
    assert.deepEqual(view.pushed[0], {
      path: "/admin/projects/project-one",
      query: { tab: "documents", item: "doc-one" },
    });
    view.state.section = "service";
    await settle();
    assert.match(calls.at(-1), /category=service.*status=active/);
  } finally {
    view.close();
  }
});

test("rows from the previous queue cannot be opened while the next queue is loading", async () => {
  let resolve;
  const delayed = new Promise((done) => {
    resolve = done;
  });
  const view = mount(
    async (path) => {
      if (path.includes("category=service")) return delayed;
      return {
        items: [
          { ...row, project_id: row.id, kind: "change", status: "submitted" },
        ],
      };
    },
    { section: "reviews" },
  );
  try {
    await settle();
    view.state.section = "service";
    await settle();
    const stale = button(view.root, "查看并处理");
    assert.ok(stale.disabled);
    stale.click();
    assert.equal(view.pushed.length, 0);
    resolve({ items: [] });
    await settle();
    assert.equal(button(view.root, "查看并处理"), undefined);
  } finally {
    view.close();
  }
});

test("project management applies customer search and the phase selected from overview", async () => {
  const calls = [];
  const view = mount(
    async (path) => {
      calls.push(path);
      return { items: [], has_more: false };
    },
    { section: "projects", initialPhase: "project" },
  );
  try {
    await settle();
    assert.match(calls[0], /phase=project/);
    const input = view.root.querySelector(".management-filters input");
    input.value = "张先生";
    input.dispatchEvent(new dom.window.Event("input"));
    view.root
      .querySelector(".management-filters")
      .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
    await settle();
    assert.equal(
      new URL(calls.at(-1), "http://localhost").searchParams.get("q"),
      "张先生",
    );
  } finally {
    view.close();
  }
});

test("creating a customer consultation retries the same request after an unknown result", async () => {
  const writes = [];
  const view = mount(async (path, init) => {
    if (path.startsWith("/api/admin/users"))
      return [
        {
          id: "alice",
          display_name: "张先生",
          username: "alice",
          role: "user",
          is_active: true,
        },
      ];
    if (init?.method === "POST") {
      writes.push(init.body);
      if (writes.length === 1) throw new client.ApiError("网络中断", 0);
      return row;
    }
    return path.endsWith("summary") ? {} : { items: [], has_more: false };
  });
  try {
    await settle();
    button(view.root, "发起客户沟通").click();
    await settle();
    const dialog = [...view.root.querySelectorAll("dialog")].find((element) =>
      element.textContent.includes("选择已有客户账号"),
    );
    const select = dialog.querySelector("select");
    select.value = "alice";
    select.dispatchEvent(new dom.window.Event("change"));
    await settle();
    dialog
      .querySelector("form")
      .dispatchEvent(new dom.window.Event("submit", { cancelable: true }));
    await settle();
    assert.ok(select.disabled);
    assert.ok(button(dialog, "关闭").disabled);
    button(dialog, "重试原操作").click();
    await settle();
    assert.equal(writes.length, 2);
    assert.equal(writes[0], writes[1]);
    assert.equal(view.pushed[0], "/admin/projects/project-one");
  } finally {
    view.close();
  }
});
