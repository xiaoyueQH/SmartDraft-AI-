import { shallowReactive } from "vue";

import { SessionClient, type User } from "./client";

const channel =
  typeof BroadcastChannel !== "undefined"
    ? new BroadcastChannel("supportflow.auth")
    : null;
export const auth = new SessionClient(
  window.sessionStorage,
  window.fetch.bind(window),
  navigator.locks || null,
  (sessionId) => channel?.postMessage({ type: "revoked", sessionId }),
);
export const authState = shallowReactive<{
  user: User | null;
  version: number;
}>({ user: null, version: 0 });
auth.subscribe(() => {
  authState.user = auth.user;
  authState.version = auth.version;
});
if (channel)
  channel.onmessage = (event) => {
    if (
      event.data?.type === "revoked" &&
      typeof event.data.sessionId === "string"
    ) {
      auth.receivedRevocation(event.data.sessionId);
    }
  };
