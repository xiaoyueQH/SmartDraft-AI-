import assert from "node:assert/strict";
import { test } from "node:test";
import * as client from "../src/auth/client.ts";
import { component, settle, vue } from "./sfc-support.mjs";

test("global tool toggle freezes an uncertain write and retries its original version and request", async () => {
  const writes = [];
  const View = component(
    "../src/views/ToolSettingsView.vue",
    {
      auth: {
        async request(path, init) {
          if (!init?.method)
            return { web_search: { enabled: false, version: 1 } };
          writes.push(init.body);
          if (writes.length === 1) throw new client.ApiError("连接中断", 0);
          return { enabled: true, version: 2 };
        },
      },
    },
    { "../auth/client": client, "vue-router": { onBeforeRouteLeave() {} } },
  );
  const root = document.createElement("div");
  document.body.append(root);
  const app = vue.createApp(View);
  app.mount(root);
  try {
    await settle();
    root.querySelector("button").click();
    await settle();
    assert.match(root.textContent, /结果待确认/);
    assert.match(root.querySelector("button").textContent, /重试原操作/);
    root.querySelector("button").click();
    await settle();
    assert.equal(writes.length, 2);
    assert.equal(writes[0], writes[1]);
    assert.equal(JSON.parse(writes[0]).enabled, true);
    assert.match(root.textContent, /已开放/);
  } finally {
    app.unmount();
    root.remove();
  }
});
