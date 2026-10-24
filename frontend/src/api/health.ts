export interface HealthSnapshot {
  api: "ok" | "unavailable";
  database: "ok" | "unavailable" | "unknown";
  checkedAt: Date;
  requestId?: string;
}

export async function checkHealth(
  signal: AbortSignal,
): Promise<HealthSnapshot> {
  const [live, ready] = await Promise.allSettled([
    fetch("/api/health/live", { signal, cache: "no-store" }),
    fetch("/api/health/ready", { signal, cache: "no-store" }),
  ]);
  if (signal.aborted) throw new DOMException("Request aborted", "AbortError");

  let api: HealthSnapshot["api"] = "unavailable";
  let database: HealthSnapshot["database"] = "unknown";
  let requestId: string | undefined;

  if (live.status === "fulfilled") {
    try {
      const body: unknown = await live.value.json();
      if (
        live.value.ok &&
        isRecord(body) &&
        body.status === "ok" &&
        body.service === "supportflow"
      )
        api = "ok";
      requestId = live.value.headers.get("X-Request-ID") || undefined;
    } catch {
      /* An HTML fallback is not a healthy API response. */
    }
  }

  if (ready.status === "fulfilled") {
    try {
      const body: unknown = await ready.value.json();
      if (isRecord(body) && isRecord(body.checks)) {
        if (
          ready.value.ok &&
          body.status === "ready" &&
          body.checks.postgresql === "ok"
        )
          database = "ok";
        else if (
          ready.value.status === 503 &&
          body.status === "not_ready" &&
          body.checks.postgresql === "unavailable"
        )
          database = "unavailable";
      }
      requestId = ready.value.headers.get("X-Request-ID") || requestId;
    } catch {
      /* Keep unknown when the proxy returns an invalid response. */
    }
  }
  return { api, database, checkedAt: new Date(), requestId };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
