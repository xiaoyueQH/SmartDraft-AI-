import assert from "node:assert/strict";
import { test } from "node:test";

import { SessionClient } from "../src/auth/client.ts";
import { component, dom, settle, vue, vueRouter } from "./sfc-support.mjs";

const sid = "10000000-0000-4000-8000-000000000001";
const user = {
  id: "alice",
  username: "alice",
  display_name: "Alice",
  role: "user",
};
const response = (body) => new Response(JSON.stringify(body));

for (const action of ["resume", "password"]) {
  test(`switch page completes ${action} login without remounting or aborting the request`, async () => {
    const saved = new Map();
    let complete, loginSignal;
    const pending = new Promise((resolve) => {
      complete = resolve;
    });
    const auth = new SessionClient(
      {
        getItem: (key) => saved.get(key) ?? null,
        setItem: (key, value) => saved.set(key, value),
        removeItem: (key) => saved.delete(key),
      },
      async (path, init) => {
        if (path.endsWith("/sessions"))
          return response([{ session_id: sid, user }]);
        loginSignal = init.signal;
        return pending;
      },
      { request: (_name, operation) => operation() },
    );
    const authState = vue.shallowReactive({ user: null, version: 0 });
    auth.subscribe(() => {
      authState.user = auth.user;
      authState.version = auth.version;
    });
    const session = { auth, authState };
    const App = component("../src/App.vue", session);
    const Login = component("../src/views/LoginView.vue", session);
    let protectedMounts = 0;
    const Private = vue.defineComponent({
      setup() {
        vue.onMounted(() => {
          protectedMounts++;
        });
        return () => vue.h("p", "private");
      },
    });
    const router = vueRouter.createRouter({
      history: vueRouter.createMemoryHistory(),
      routes: [
        { path: "/login", component: Login },
        { path: "/", component: Private, meta: { requiresAuth: true } },
      ],
    });
    await router.push("/login?switch=1");
    await router.isReady();
    const root = document.createElement("div");
    document.body.append(root);
    const app = vue.createApp(App);
    try {
      app.use(router).mount(root);
      await settle();
      assert.equal(
        auth.version,
        1,
        "mount must clear the old identity only once",
      );
      const originalForm = root.querySelector("form");
      if (action === "resume") {
        root.querySelector('button[type="button"]').click();
      } else {
        for (const [id, value] of [
          ["username", "alice"],
          ["password", "password"],
        ]) {
          const input = root.querySelector(`#${id}`);
          input.value = value;
          input.dispatchEvent(new dom.window.Event("input", { bubbles: true }));
        }
        originalForm.dispatchEvent(
          new dom.window.Event("submit", { bubbles: true, cancelable: true }),
        );
      }
      await settle();
      assert.equal(root.querySelector("form"), originalForm);
      assert.equal(loginSignal.aborted, false);
      assert.equal(root.querySelector('button[type="submit"]').disabled, true);
      complete(response({ session_id: sid, access_token: "access", user }));
      await settle();
      assert.equal(router.currentRoute.value.path, "/");
      assert.equal(authState.user.username, "alice");
      assert.equal(protectedMounts, 1);
      authState.version++;
      await settle();
      assert.equal(
        protectedMounts,
        2,
        "private pages must still reset on identity changes",
      );
    } finally {
      app.unmount();
      root.remove();
    }
  });
}
