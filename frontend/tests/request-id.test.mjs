import assert from "node:assert/strict";
import { test } from "node:test";
import { requestId } from "../src/api/request-id.ts";

function withCrypto(t, value) {
  const original = Object.getOwnPropertyDescriptor(globalThis, "crypto");
  Object.defineProperty(globalThis, "crypto", { configurable: true, value });
  t.after(() => {
    if (original) Object.defineProperty(globalThis, "crypto", original);
    else delete globalThis.crypto;
  });
}

test("request IDs prefer the native method and retain its receiver", (t) => {
  const native = {
    randomUUID() {
      assert.equal(this, native);
      return "10000000-0000-4000-8000-000000000001";
    },
  };
  withCrypto(t, native);
  assert.equal(requestId(), "10000000-0000-4000-8000-000000000001");
});

test("HTTP-like crypto without randomUUID generates an RFC UUID from secure bytes", (t) => {
  const source = {
    getRandomValues(bytes) {
      assert.equal(this, source);
      bytes.set(Array.from({ length: 16 }, (_, i) => i));
      return bytes;
    },
  };
  withCrypto(t, source);
  assert.equal(requestId(), "00010203-0405-4607-8809-0a0b0c0d0e0f");
});

test("fallback generates fresh request keys without Math.random", (t) => {
  const secure = globalThis.crypto;
  withCrypto(t, { getRandomValues: (bytes) => secure.getRandomValues(bytes) });
  const values = Array.from({ length: 256 }, requestId);
  assert.equal(new Set(values).size, values.length);
  assert.ok(
    values.every((value) =>
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
        value,
      ),
    ),
  );
});

test("unavailable secure randomness fails with an actionable message", (t) => {
  withCrypto(t, undefined);
  assert.throws(requestId, /更新浏览器.*HTTPS/);
});
