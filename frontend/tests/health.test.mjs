import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { checkHealth } from "../src/api/health.ts";

const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
});

function reply(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json",
      "X-Request-ID": "test-request",
    },
  });
}

test("reports a valid live API and ready database", async () => {
  globalThis.fetch = async (url) =>
    url.endsWith("/live")
      ? reply({ status: "ok", service: "supportflow" })
      : reply({ status: "ready", checks: { postgresql: "ok" } });
  const result = await checkHealth(new AbortController().signal);
  assert.equal(result.api, "ok");
  assert.equal(result.database, "ok");
  assert.equal(result.requestId, "test-request");
});

test("keeps the API live when the database returns 503", async () => {
  globalThis.fetch = async (url) =>
    url.endsWith("/live")
      ? reply({ status: "ok", service: "supportflow" })
      : reply(
          { status: "not_ready", checks: { postgresql: "unavailable" } },
          503,
        );
  const result = await checkHealth(new AbortController().signal);
  assert.equal(result.api, "ok");
  assert.equal(result.database, "unavailable");
});

test("does not mistake HTML fallbacks or malformed payloads for a healthy service", async () => {
  globalThis.fetch = async () => new Response("<html>Proxy fallback</html>");
  let result = await checkHealth(new AbortController().signal);
  assert.equal(result.api, "unavailable");
  assert.equal(result.database, "unknown");
  globalThis.fetch = async () =>
    reply({ status: "ready", checks: { postgresql: "wrong" } });
  result = await checkHealth(new AbortController().signal);
  assert.equal(result.api, "unavailable");
  assert.equal(result.database, "unknown");
});

test("handles an unavailable proxy without inventing a database result", async () => {
  globalThis.fetch = async () => {
    throw new TypeError("Network error");
  };
  const result = await checkHealth(new AbortController().signal);
  assert.equal(result.api, "unavailable");
  assert.equal(result.database, "unknown");
});

test("rejects cancelled requests", async () => {
  const controller = new AbortController();
  controller.abort();
  globalThis.fetch = async () => {
    throw new DOMException("Aborted", "AbortError");
  };
  await assert.rejects(checkHealth(controller.signal), { name: "AbortError" });
});
