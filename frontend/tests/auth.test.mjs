import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, SESSION_KEY, SessionClient } from "../src/auth/client.ts";

function createClient(
  storage,
  fetcher,
  locks = { request: (_name, operation) => operation() },
  publish = undefined,
) {
  return new SessionClient(storage, fetcher, locks, publish);
}

const A = "10000000-0000-4000-8000-000000000001";
const B = "10000000-0000-4000-8000-000000000002";
const user = (name) => ({
  id: name,
  username: name,
  display_name: name,
  role: "user",
  is_active: true,
  created_at: "",
});
const tokens = (name, sid, access = name) => ({
  user: user(name),
  session_id: sid,
  access_token: access,
});
const response = (body, status = 200) =>
  new Response(status === 204 ? null : JSON.stringify(body), { status });
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};
function storage() {
  const values = new Map();
  return {
    values,
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

test("tab storage persists only a session locator, never either token", async () => {
  const saved = storage();
  const client = createClient(saved, async () =>
    response(tokens("alice", A, "secret-access")),
  );
  await client.login("alice", "password");
  assert.deepEqual([...saved.values], [[SESSION_KEY, A]]);
  assert.equal(client.user.username, "alice");
});

test("two tabs independently restore different sessions in one browser", async () => {
  const fetcher = async (_url, init) => {
    const sid = init.headers.get("X-Session-ID");
    return response(sid === A ? tokens("alice", A) : tokens("bob", B));
  };
  const first = storage(),
    second = storage();
  first.setItem(SESSION_KEY, A);
  second.setItem(SESSION_KEY, B);
  const alice = createClient(first, fetcher),
    bob = createClient(second, fetcher);
  await Promise.all([alice.ensureSession(), bob.ensureSession()]);
  assert.equal(alice.user.username, "alice");
  assert.equal(bob.user.username, "bob");
  bob.receivedRevocation(A);
  assert.equal(bob.user.username, "bob");
  alice.receivedRevocation(A);
  assert.equal(alice.user, null);
});

test("late old-account responses cannot populate the new account", async () => {
  const pending = deferred();
  const client = createClient(storage(), async (url, init) => {
    if (url.endsWith("/login"))
      return response(
        JSON.parse(init.body).username === "alice"
          ? tokens("alice", A)
          : tokens("bob", B),
      );
    return pending.promise;
  });
  await client.login("alice", "password");
  const request = client.request("/api/private");
  await new Promise((done) => setImmediate(done));
  await client.login("bob", "password");
  pending.resolve(response({ private: "alice-data" }));
  await assert.rejects(request, (error) => error.code === "SESSION_CHANGED");
  assert.equal(client.user.username, "bob");
});

test("an old refresh cannot restore an account after switching", async () => {
  const pending = deferred();
  const client = createClient(storage(), async (url, init) => {
    if (url.endsWith("/refresh")) return pending.promise;
    return response(
      JSON.parse(init.body).username === "alice"
        ? tokens("alice", A)
        : tokens("bob", B),
    );
  });
  await client.login("alice", "password");
  const refresh = client.refresh();
  await client.login("bob", "password");
  pending.resolve(response(tokens("alice", A)));
  await assert.rejects(refresh, (error) => error.code === "SESSION_CHANGED");
  assert.equal(client.user.username, "bob");
});

test("concurrent expired-access responses share one refresh and retry once", async () => {
  let refreshCount = 0;
  const client = createClient(storage(), async (url, init) => {
    if (url.endsWith("/login")) return response(tokens("alice", A, "expired"));
    if (url.endsWith("/refresh")) {
      refreshCount++;
      await new Promise((done) => setTimeout(done, 5));
      return response(tokens("alice", A, "renewed"));
    }
    return init.headers.get("Authorization") === "Bearer renewed"
      ? response({ ok: true })
      : response({ error: { code: "ACCESS_EXPIRED" } }, 401);
  });
  await client.login("alice", "password");
  const results = await Promise.all(
    Array.from({ length: 10 }, () => client.request("/api/private")),
  );
  assert.equal(refreshCount, 1);
  assert.ok(results.every((result) => result.ok));
});

test("an operation started under A is never sent using B credentials", async () => {
  let writes = 0;
  const client = createClient(storage(), async (url, init) => {
    if (url.endsWith("/login"))
      return response(
        JSON.parse(init.body).username === "alice"
          ? tokens("alice", A)
          : tokens("bob", B),
      );
    writes++;
    return response({ ok: true });
  });
  await client.login("alice", "password");
  const originalEnsure = client.ensureSession.bind(client);
  const resume = deferred();
  client.ensureSession = async () => {
    await resume.promise;
    return originalEnsure();
  };
  const write = client.request("/api/submit", { method: "POST", body: "{}" });
  await client.login("bob", "password");
  resume.resolve();
  await assert.rejects(write, (error) => error.code === "SESSION_CHANGED");
  assert.equal(writes, 0);
});

test("late logout response does not clear a subsequent login", async () => {
  const pending = deferred();
  const client = createClient(storage(), async (url, init) => {
    if (url.endsWith("/logout")) return pending.promise;
    return response(
      JSON.parse(init.body).username === "alice"
        ? tokens("alice", A)
        : tokens("bob", B),
    );
  });
  await client.login("alice", "password");
  const logout = client.logout();
  await client.login("bob", "password");
  pending.resolve(response(null, 204));
  await logout;
  assert.equal(client.user.username, "bob");
});

test("network failure during refresh does not erase the session locator", async () => {
  const saved = storage();
  saved.setItem(SESSION_KEY, A);
  const client = createClient(saved, async () => {
    throw new TypeError("Network unavailable");
  });
  await assert.rejects(client.ensureSession());
  assert.equal(saved.getItem(SESSION_KEY), A);
});

test("revoked session is cleared and a forbidden action is not refreshed", async () => {
  let count = 0;
  const client = createClient(storage(), async (url) => {
    if (url.endsWith("/login")) return response(tokens("alice", A));
    if (url.endsWith("/refresh")) {
      count++;
      throw new Error("must not refresh");
    }
    return response(
      { error: { code: "FORBIDDEN", message: "Forbidden" } },
      403,
    );
  });
  await client.login("alice", "password");
  await assert.rejects(client.request("/api/admin/users"), ApiError);
  assert.equal(count, 0);
  assert.equal(client.user.username, "alice");
});

test("unsupported cross-tab locking never sends an unsafe refresh", async () => {
  const saved = storage();
  saved.setItem(SESSION_KEY, A);
  let requests = 0;
  const client = createClient(
    saved,
    async () => {
      requests++;
    },
    null,
  );
  await assert.rejects(
    client.refresh(),
    (error) => error.code === "SESSION_LOCK_UNAVAILABLE",
  );
  assert.equal(requests, 0);
  assert.equal(saved.getItem(SESSION_KEY), A);
});

test("two tabs sharing a session serialize refresh through the shared lock", async () => {
  let tail = Promise.resolve(),
    running = 0,
    maxRunning = 0;
  const locks = {
    request: (_key, operation) => {
      const next = tail.then(operation);
      tail = next.catch(() => {});
      return next;
    },
  };
  const fetcher = async () => {
    running++;
    maxRunning = Math.max(maxRunning, running);
    await new Promise((done) => setTimeout(done, 5));
    running--;
    return response(tokens("alice", A));
  };
  const first = storage(),
    second = storage();
  first.setItem(SESSION_KEY, A);
  second.setItem(SESSION_KEY, A);
  const alice = createClient(first, fetcher, locks),
    clone = createClient(second, fetcher, locks);
  await Promise.all([alice.refresh(), clone.refresh()]);
  assert.equal(maxRunning, 1);
  assert.equal(alice.user.username, "alice");
  assert.equal(clone.user.username, "alice");
});

test("a new tab can select a remembered account without persisting tokens", async () => {
  const saved = storage();
  const client = createClient(saved, async (url) =>
    url.endsWith("/sessions")
      ? response([{ session_id: A, user: user("alice") }])
      : response(tokens("alice", A)),
  );
  const candidates = await client.availableSessions();
  assert.equal(await client.ensureSession(), null);
  await client.resume(candidates[0].session_id);
  assert.equal(client.user.username, "alice");
  assert.deepEqual([...saved.values], [[SESSION_KEY, A]]);
});

test("account chooser waits for logout commit and only then notifies sibling tabs", async () => {
  const pending = deferred(),
    published = [];
  let listed = false;
  const client = createClient(
    storage(),
    async (url) => {
      if (url.endsWith("/login")) return response(tokens("alice", A));
      if (url.endsWith("/logout")) return pending.promise;
      listed = true;
      return response([]);
    },
    { request: (_key, operation) => operation() },
    (sid) => published.push(sid),
  );
  await client.login("alice", "password");
  const logout = client.logout();
  const chooser = client.availableSessions();
  await new Promise((done) => setImmediate(done));
  assert.equal(listed, false);
  assert.deepEqual(published, []);
  pending.resolve(response(null, 204));
  await logout;
  assert.deepEqual(await chooser, []);
  assert.deepEqual(published, [A]);
});

// The stream uses this tab's headers, including when several accounts share a browser.
test("stream decodes fragmented UTF-8 frames before the response ends", async () => {
  let source;
  let requestHeaders;
  const client = createClient(storage(), async (url, init) => {
    if (url.endsWith("/login")) return response(tokens("alice", A));
    requestHeaders = init.headers;
    return new Response(
      new ReadableStream({
        start(controller) {
          source = controller;
        },
      }),
      {
        headers: { "Content-Type": "text/event-stream", "X-Auth-Session": A },
      },
    );
  });
  await client.login("alice", "password");
  const frames = [];
  const operation = client.stream(
    "/api/projects/one/runs/run/stream",
    (event, value) => frames.push([event, value]),
    new AbortController().signal,
  );
  while (!source) await new Promise((resolve) => setImmediate(resolve));
  const packet = new TextEncoder().encode(
    'event: run\r\ndata: {"text":"正在回复"}\r\n\r\n',
  );
  for (const byte of packet) source.enqueue(Uint8Array.of(byte));
  while (!frames.length) await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(frames, [["run", { text: "正在回复" }]]);
  assert.equal(requestHeaders.get("Authorization"), "Bearer alice");
  assert.equal(requestHeaders.get("X-Session-ID"), A);
  source.close();
  await operation;
});

test("a late stream frame from the old account is discarded after switching", async () => {
  let source;
  const client = createClient(storage(), async (url, init) => {
    if (url.endsWith("/login"))
      return response(
        JSON.parse(init.body).username === "alice"
          ? tokens("alice", A)
          : tokens("bob", B),
      );
    return new Response(
      new ReadableStream({
        start(controller) {
          source = controller;
        },
      }),
      { headers: { "Content-Type": "text/event-stream" } },
    );
  });
  await client.login("alice", "password");
  const frames = [];
  const operation = client.stream(
    "/api/projects/one/runs/run/stream",
    (_, value) => frames.push(value),
    new AbortController().signal,
  );
  const rejected = assert.rejects(
    operation,
    (error) => error.code === "SESSION_CHANGED",
  );
  while (!source) await new Promise((resolve) => setImmediate(resolve));
  await client.login("bob", "password");
  source.enqueue(
    new TextEncoder().encode('event: run\ndata: {"text":"alice-private"}\n\n'),
  );
  await rejected;
  assert.deepEqual(frames, []);
  assert.equal(client.user.username, "bob");
});

test("access expiry inside a stream renews the session without resubmitting a message", async () => {
  const calls = [];
  const client = createClient(storage(), async (url) => {
    calls.push(url);
    if (url.endsWith("/login")) return response(tokens("alice", A));
    if (url.endsWith("/refresh"))
      return response(tokens("alice", A, "renewed"));
    return new Response(
      'event: error\ndata: {"code":"ACCESS_EXPIRED","status":401,"message":"expired"}\n\n',
      { headers: { "Content-Type": "text/event-stream" } },
    );
  });
  await client.login("alice", "password");
  await client.stream(
    "/api/projects/one/runs/run/stream",
    () => {},
    new AbortController().signal,
  );
  assert.equal(client.user.username, "alice");
  assert.equal(calls.filter((url) => url.endsWith("/refresh")).length, 1);
  assert.equal(
    calls.some((url) => url.endsWith("/messages")),
    false,
  );
});
