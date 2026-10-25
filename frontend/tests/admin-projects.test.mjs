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

test("admin landing page shows real business totals and customer records instead of a chat", async () => {
  const view = mount(async (path) =>
    path.endsWith("/summary")
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
