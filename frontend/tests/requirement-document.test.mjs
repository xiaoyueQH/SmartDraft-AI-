import assert from "node:assert/strict";
import { test } from "node:test";
import { marked } from "marked";
import createDOMPurify from "dompurify";
import { component, dom, settle, vue } from "./sfc-support.mjs";

test("document preview removes only adjacent duplicate headings and preserves lists, code and sanitation", async () => {
  const root = document.createElement("div");
  document.body.append(root);
  const View = component(
    "../src/components/RequirementDocument.vue",
    {},
    {
      marked: { marked },
      dompurify: { default: createDOMPurify(dom.window) },
      mermaid: {
        default: {
          initialize() {},
          async render() {
            return { svg: "" };
          },
        },
      },
    },
  );
  const content =
    "## 角色权限\n\n### 角色权限\n\n- 操作人：员工\n- 步骤：\n  1. 查看空闲\n  2. 提交预约\n\n" +
    "```python\nif ready:\n    save()\n```\n\n## 角色权限\n\n第二处独立说明\n\n" +
    "<script>alert(1)</script>\n<img src='https://example.org/pixel.png'>\n[不安全链接](javascript:alert(1))\n";
  const app = vue.createApp({ render: () => vue.h(View, { content }) });
  try {
    app.mount(root);
    await settle();
    assert.equal(root.querySelectorAll("h2").length, 2);
    assert.equal(root.querySelectorAll("h3").length, 0);
    assert.deepEqual(
      [...root.querySelectorAll("li > ol > li")].map((li) =>
        li.textContent.trim(),
      ),
      ["查看空闲", "提交预约"],
    );
    assert.match(root.querySelector("pre code").textContent, /\n    save\(\)/);
    assert.equal(
      root.querySelector("script, img, a[href^='javascript:']"),
      null,
    );
    assert.match(root.textContent, /第二处独立说明/);
  } finally {
    app.unmount();
    root.remove();
  }
});
