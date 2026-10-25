import assert from "node:assert/strict";
import { test } from "node:test";
import * as projects from "../src/api/projects.ts";
import { component, dom, settle, vue } from "./sfc-support.mjs";
import { marked } from "marked";
import createDOMPurify from "dompurify";

const agentRun = (id, extra = {}) => ({
  id,
  prompt: "我想管理预约",
  answer: "",
  status: "running",
  created_at: "2026-10-25T00:00:00Z",
  events: [],
  ...extra,
});
function mount(extra = {}) {
  const root = document.createElement("div");
  document.body.append(root);
  const state = vue.reactive({
    projectId: "one",
    messages: [],
    runs: [],
    modelValue: "",
    configured: true,
    locked: false,
    sending: false,
    outgoing: "",
    ...extra,
  });
  let sent = 0;
  const View = component(
    "../src/components/ConversationPanel.vue",
    {},
    {
      "../api/projects": projects,
      marked: { marked },
      dompurify: { default: createDOMPurify(dom.window) },
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
  const app = vue.createApp({
    render: () =>
      vue.h(View, {
        ...state,
        "onUpdate:modelValue": (value) => {
          state.modelValue = value;
        },
        onSend: () => {
          sent++;
        },
      }),
  });
  app.mount(root);
  return {
    root,
    state,
    sent: () => sent,
    close() {
      app.unmount();
      root.remove();
    },
  };
}

test("idle conversation has no execution panel and keeps the composer separate from scroll history", async () => {
  const view = mount();
  try {
    await settle();
    assert.equal(view.root.querySelector(".chat-execution"), null);
    assert.equal(view.root.querySelector(".project-trace"), null);
    assert.match(view.root.textContent, /您目前主要做什么业务/);
    assert.equal(
      view.root.querySelector(".chat-viewport .project-composer"),
      null,
    );
  } finally {
    view.close();
  }
});

test("thinking expands real steps and completion retains the exact trace beside its answer", async () => {
  const run = agentRun("run-1");
  const view = mount({ runs: [run] });
  try {
    await settle();
    assert.equal(view.root.querySelectorAll(".project-message.user").length, 1);
    const trace = view.root.querySelector("details.chat-execution");
    assert.equal(trace.open, false);
    assert.match(trace.querySelector("summary").textContent, /正在思考/);
    trace.querySelector("summary").click();
    assert.ok(trace.open);
    view.state.runs[0].events.push({
      sequence: 1,
      type: "tool",
      tool: "remember_requirements",
      summary: "已整理沟通纪要",
      ok: true,
      duration_ms: 12,
    });
    await settle();
    assert.equal(view.root.querySelector("details"), trace);
    assert.match(trace.textContent, /remember_requirements/);
    assert.doesNotMatch(trace.textContent, /Skill|MCP/);
    view.state.runs[0].status = "completed";
    view.state.messages = [
      {
        id: "user-1",
        role: "user",
        content: run.prompt,
        run_id: run.id,
        created_at: run.created_at,
      },
      {
        id: "assistant-1",
        role: "assistant",
        content: "谁负责预约？",
        run_id: run.id,
        created_at: run.created_at,
      },
    ];
    await settle();
    assert.equal(view.root.querySelectorAll(".project-message.user").length, 1);
    assert.equal(view.root.querySelectorAll(".chat-execution").length, 1);
    assert.equal(view.root.querySelector(".chat-execution"), trace);
    assert.ok(trace.open);
    assert.match(
      view.root.querySelector(".project-message.assistant").textContent,
      /已完成.*remember_requirements.*谁负责预约/s,
    );
    const second = agentRun("run-2", { created_at: "2026-10-25T00:01:00Z" });
    view.state.runs.push(second);
    await settle();
    assert.equal(view.root.querySelectorAll(".project-message.user").length, 2);
    assert.equal(view.root.querySelectorAll(".chat-execution").length, 2);
    assert.equal(view.root.querySelectorAll(".execution-kind").length, 1);
  } finally {
    view.close();
  }
});

test("failed execution is shown in its turn without manufacturing skill or MCP calls", async () => {
  const view = mount({
    runs: [
      agentRun("failed", {
        status: "failed",
        answer: "模型服务暂不可用",
        events: [
          {
            sequence: 1,
            type: "tool",
            tool: "read_document",
            summary: "读取未完成",
            ok: false,
          },
        ],
      }),
    ],
  });
  try {
    await settle();
    assert.match(
      view.root.querySelector(".chat-error").textContent,
      /模型服务暂不可用/,
    );
    assert.match(view.root.querySelector("summary").textContent, /执行未完成/);
    assert.match(
      view.root.querySelector(".execution-failed").textContent,
      /未成功/,
    );
  } finally {
    view.close();
  }
});

test("unconfirmed sending stays distinct from actual model execution and does not duplicate accepted prompts", async () => {
  const view = mount({ outgoing: "我想管理预约", sending: true, locked: true });
  try {
    await settle();
    assert.match(view.root.textContent, /正在发送/);
    assert.equal(view.root.querySelector(".chat-execution"), null);
    view.state.sending = false;
    await settle();
    assert.match(view.root.textContent, /发送结果待确认/);
    view.state.outgoing = "";
    view.state.runs = [agentRun("accepted")];
    await settle();
    assert.equal(view.root.querySelectorAll(".project-message.user").length, 1);
    assert.match(view.root.querySelector("summary").textContent, /正在思考/);
  } finally {
    view.close();
  }
});

test("Enter sends but IME composition and Shift Enter retain normal editing", async () => {
  const view = mount({ modelValue: "业务情况" });
  try {
    await settle();
    const input = view.root.querySelector("textarea");
    input.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", {
        key: "Enter",
        shiftKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    input.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", {
        key: "Enter",
        isComposing: true,
        bubbles: true,
        cancelable: true,
      }),
    );
    assert.equal(view.sent(), 0);
    input.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", {
        key: "Enter",
        bubbles: true,
        cancelable: true,
      }),
    );
    assert.equal(view.sent(), 1);
    view.state.runs = [agentRun("busy")];
    await settle();
    input.dispatchEvent(
      new dom.window.KeyboardEvent("keydown", {
        key: "Enter",
        bubbles: true,
        cancelable: true,
      }),
    );
    assert.equal(view.sent(), 1);
  } finally {
    view.close();
  }
});

test("old messages without a run relation are preserved without guessing a trace", async () => {
  const view = mount({
    messages: [
      { id: "old-user", role: "user", content: "旧问题" },
      { id: "old-assistant", role: "assistant", content: "旧答复" },
    ],
    runs: [
      agentRun("old", {
        status: "completed",
        prompt: "旧问题",
        answer: "旧答复",
      }),
    ],
  });
  try {
    await settle();
    assert.equal(view.root.querySelectorAll(".project-message").length, 2);
    assert.match(view.root.textContent, /旧问题.*旧答复/s);
    assert.equal(view.root.querySelector(".chat-execution"), null);
  } finally {
    view.close();
  }
});
