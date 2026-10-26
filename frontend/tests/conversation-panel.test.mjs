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
    assert.match(trace.textContent, /整理业务信息/);
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
      /已完成.*整理业务信息.*谁负责预约/s,
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
    assert.match(view.root.querySelector("summary").textContent, /暂未完成/);
    assert.match(
      view.root.querySelector(".execution-failed").textContent,
      /暂未完成/,
    );
  } finally {
    view.close();
  }
});

test("a completed conversation with a failed record does not claim all steps succeeded", async () => {
  const view = mount({
    messages: [
      {
        id: "partial-user",
        role: "user",
        content: "登记订单",
        run_id: "partial-record",
      },
      {
        id: "partial-answer",
        role: "assistant",
        content: "原话已保留，纪要待核对。",
        run_id: "partial-record",
      },
    ],
    runs: [
      agentRun("partial-record", {
        status: "completed",
        answer: "原话已保留，纪要待核对。",
        events: [
          {
            sequence: 1,
            type: "tool",
            tool: "record_consultation",
            ok: false,
            summary: "记录失败",
          },
        ],
      }),
    ],
  });
  try {
    await settle();
    assert.match(
      view.root.querySelector("summary").textContent,
      /部分内容待完善/,
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

test("the user research switch disappears when administrator availability is removed", async () => {
  const view = mount({ searchAvailable: true, webSearch: false });
  try {
    await settle();
    assert.ok(view.root.querySelector(".web-search-option input"));
    view.state.searchAvailable = false;
    await settle();
    assert.equal(view.root.querySelector(".web-search-option"), null);
  } finally {
    view.close();
  }
});

test("execution details use business labels and hide internal terminology", async () => {
  const view = mount({
    runs: [
      agentRun("plain", {
        events: [
          {
            sequence: 1,
            type: "node",
            node: "hitl",
            summary: "LangGraph interrupt HITL 人工断点",
          },
          {
            sequence: 2,
            type: "tool",
            tool: "save_draft",
            summary: "JSON schema 校验完成",
            duration_ms: 2500,
            tokens: { total_tokens: 9000 },
          },
          {
            sequence: 3,
            type: "model",
            summary: "extract node 3",
            duration_ms: 1000,
          },
        ],
        status: "failed",
        answer: "稍后重试",
      }),
    ],
  });
  try {
    await settle();
    const text = view.root.querySelector(".chat-execution").textContent;
    assert.match(text, /沟通已保存，等待您继续/);
    assert.match(text, /整理需求草案/);
    assert.match(text, /用时 2.5 秒/);
    assert.doesNotMatch(
      text,
      /LangGraph|interrupt|HITL|人工断点|JSON|schema|save_draft|tokens|9000|extract node/,
    );
  } finally {
    view.close();
  }
});

test("live text is shown before saving and replaced by one stored reply on completion", async () => {
  const run = agentRun("run-live", {
    preview: { text: "先从预约登记", kind: "reply", title: "" },
  });
  const view = mount({ runs: [run] });
  try {
    await settle();
    assert.equal(
      view.root.querySelector(".chat-stream-text").textContent,
      "先从预约登记",
    );
    assert.match(view.root.textContent, /内容尚在整理/);
    view.state.runs = [
      { ...run, preview: { ...run.preview, text: "先从预约登记开始。" } },
    ];
    await settle();
    assert.equal(
      view.root.querySelector(".chat-stream-text").textContent,
      "先从预约登记开始。",
    );
    view.state.runs = [
      {
        ...run,
        status: "completed",
        answer: "先从预约登记开始。",
        preview: null,
      },
    ];
    view.state.messages = [
      { id: "user", run_id: run.id, role: "user", content: run.prompt },
      {
        id: "assistant",
        run_id: run.id,
        role: "assistant",
        content: "先从预约登记开始。",
      },
    ];
    await settle();
    assert.equal(view.root.querySelector(".chat-stream-text"), null);
    assert.equal(
      view.root.querySelectorAll(".project-message.assistant").length,
      1,
    );
    assert.equal(
      view.root.textContent.split("先从预约登记开始。").length - 1,
      1,
    );
  } finally {
    view.close();
  }
});

test("unfinished chapter preview stays plain text and never executes HTML", async () => {
  const view = mount({
    runs: [
      agentRun("chapter", {
        preview: {
          kind: "document",
          title: "角色与权限",
          text: '<img src=x onerror="alert(1)">\n## 客户',
        },
      }),
    ],
  });
  try {
    await settle();
    assert.match(view.root.textContent, /正在撰写：角色与权限/);
    assert.equal(view.root.querySelector(".chat-live-preview img"), null);
    view.state.runs = [
      { ...view.state.runs[0], status: "failed", answer: "本轮暂未完成" },
    ];
    await settle();
    assert.equal(view.root.querySelector(".chat-live-preview"), null);
  } finally {
    view.close();
  }
});
