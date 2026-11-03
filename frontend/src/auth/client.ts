export interface User {
  id: string;
  username: string;
  display_name: string;
  role: "admin" | "staff" | "user";
  is_active: boolean;
  created_at: string;
}

interface Tokens {
  access_token: string;
  session_id: string;
  user: User;
}

export interface SavedLogin {
  session_id: string;
  user: User;
}

interface TabStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

interface Locks {
  request<T>(name: string, callback: () => Promise<T>): Promise<T>;
}

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status = 0, code = "REQUEST_FAILED") {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export const SESSION_KEY = "supportflow.login-session";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export class SessionClient {
  user: User | null = null;
  version = 0;
  sessionId: string | null;
  private accessToken: string | null = null;
  private controller = new AbortController();
  private refreshFlight: Promise<User> | null = null;
  private logoutFlight: Promise<void> | null = null;
  private listeners = new Set<() => void>();
  private storage: TabStorage;
  private fetcher: typeof fetch;
  private locks: Locks | null;
  private publish: (sid: string) => void;

  constructor(
    storage: TabStorage,
    fetcher: typeof fetch,
    locks: Locks | null = null,
    publish: (sid: string) => void = () => {},
  ) {
    this.storage = storage;
    this.fetcher = fetcher;
    this.locks = locks;
    this.publish = publish;
    const saved = storage.getItem(SESSION_KEY);
    this.sessionId = saved && UUID.test(saved) ? saved : null;
    if (saved && !this.sessionId) storage.removeItem(SESSION_KEY);
  }

  subscribe(listener: () => void) {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  private changed() {
    this.listeners.forEach((listener) => listener());
  }

  private assertCurrent(version: number) {
    if (version !== this.version)
      throw new ApiError("账号已切换，请重新操作", 0, "SESSION_CHANGED");
  }

  private clear(keepLocator = false) {
    this.version++;
    this.controller.abort();
    this.controller = new AbortController();
    this.refreshFlight = null;
    this.accessToken = null;
    this.user = null;
    if (!keepLocator) {
      this.sessionId = null;
      this.storage.removeItem(SESSION_KEY);
    }
    this.changed();
  }

  prepareSwitch() {
    this.clear(true);
  }

  receivedRevocation(sid: string) {
    if (sid === this.sessionId) this.clear();
  }

  private async raw(
    path: string,
    init: RequestInit,
    sid: string | null,
    token: string | null,
    useCurrentSignal = true,
    streaming = false,
  ) {
    const headers = new Headers(init.headers);
    if (sid) headers.set("X-Session-ID", sid);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (init.body && !headers.has("Content-Type"))
      headers.set("Content-Type", "application/json");
    const signals = [AbortSignal.timeout(streaming ? 35000 : 10000)];
    if (useCurrentSignal) signals.push(this.controller.signal);
    if (init.signal) signals.push(init.signal);
    try {
      return await this.fetcher(path, {
        ...init,
        headers,
        credentials: "same-origin",
        cache: "no-store",
        signal: AbortSignal.any(signals),
      });
    } catch (error) {
      if (
        error instanceof TypeError ||
        (error instanceof Error && error.name === "TimeoutError")
      )
        throw new ApiError(
          "连接暂时中断，请稍后重试原操作",
          0,
          "NETWORK_UNAVAILABLE",
        );
      throw error;
    }
  }

  private async read<T>(response: Response, version: number): Promise<T> {
    let body;
    try {
      body = response.status === 204 ? null : await response.json();
    } catch {
      this.assertCurrent(version);
      throw new ApiError(
        response.ok
          ? "服务返回内容不完整，请重试原操作"
          : "服务暂时不可用，请稍后重试原操作",
        // A successful response without its receipt has an unknown write outcome.
        // Keep the original request available for retry instead of creating another.
        response.ok ? 0 : response.status,
        response.ok ? "RESPONSE_INVALID" : "REQUEST_FAILED",
      );
    }
    this.assertCurrent(version);
    if (!response.ok)
      throw new ApiError(
        body?.error?.message || "请求未能完成",
        response.status,
        body?.error?.code || "REQUEST_FAILED",
      );
    const responseSid = response.headers.get("X-Auth-Session");
    if (responseSid && responseSid !== this.sessionId) {
      throw new ApiError("账号会话不匹配", 401, "SESSION_INVALID");
    }
    return body as T;
  }

  private accept(tokens: Tokens, expectedSid?: string) {
    if (
      !UUID.test(tokens.session_id) ||
      !tokens.access_token ||
      !tokens.user?.id ||
      (expectedSid && tokens.session_id !== expectedSid) ||
      (expectedSid && this.user && tokens.user.id !== this.user.id)
    ) {
      throw new ApiError("登录响应不匹配", 401, "SESSION_INVALID");
    }
    const identityChanged =
      this.sessionId !== tokens.session_id ||
      this.user?.id !== tokens.user.id ||
      this.user?.role !== tokens.user.role;
    this.sessionId = tokens.session_id;
    this.accessToken = tokens.access_token;
    this.user = tokens.user;
    this.storage.setItem(SESSION_KEY, tokens.session_id);
    if (identityChanged) this.version++;
    this.changed();
    return tokens.user;
  }

  async login(username: string, password: string): Promise<User> {
    const oldSid = this.sessionId;
    this.prepareSwitch();
    const version = this.version;
    const response = await this.raw(
      "/api/auth/login",
      {
        method: "POST",
        body: JSON.stringify({ username, password }),
      },
      oldSid,
      null,
    );
    const tokens = await this.read<Tokens>(response, version);
    const user = this.accept(tokens);
    if (oldSid && oldSid !== this.sessionId) this.publish(oldSid);
    return user;
  }

  async refresh(): Promise<User> {
    if (this.refreshFlight) return this.refreshFlight;
    if (!this.locks)
      throw new ApiError(
        "当前连接无法安全续期。请使用 HTTPS 或本机 localhost 地址访问；若已使用安全连接，请更新浏览器。",
        0,
        "SESSION_LOCK_UNAVAILABLE",
      );
    const sid = this.sessionId;
    if (!sid) throw new ApiError("请先登录", 401, "LOGIN_REQUIRED");
    const version = this.version;
    const operation = async () => {
      this.assertCurrent(version);
      const response = await this.raw(
        "/api/auth/refresh",
        { method: "POST", body: "{}" },
        sid,
        null,
      );
      return this.accept(await this.read<Tokens>(response, version), sid);
    };
    const flight = this.locks.request(`supportflow.refresh.${sid}`, operation);
    this.refreshFlight = flight;
    try {
      return await flight;
    } catch (error) {
      if (
        version === this.version &&
        error instanceof ApiError &&
        error.status === 401
      ) {
        this.clear();
        this.publish(sid);
      }
      throw error;
    } finally {
      if (this.refreshFlight === flight) this.refreshFlight = null;
    }
  }

  async ensureSession(): Promise<User | null> {
    if (this.user && this.accessToken) return this.user;
    if (!this.sessionId) return null;
    try {
      return await this.refresh();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return null;
      throw error;
    }
  }

  async availableSessions(): Promise<SavedLogin[]> {
    const version = this.version;
    if (this.logoutFlight) await this.logoutFlight;
    this.assertCurrent(version);
    return this.read<SavedLogin[]>(
      await this.raw(
        "/api/auth/sessions",
        { method: "POST", body: "{}" },
        null,
        null,
      ),
      version,
    );
  }

  async resume(sid: string): Promise<User> {
    if (!UUID.test(sid)) throw new ApiError("登录会话无效");
    this.clear();
    this.sessionId = sid;
    this.storage.setItem(SESSION_KEY, sid);
    return this.refresh();
  }

  async request<T>(
    path: string,
    init: RequestInit = {},
    onFrame?: (event: string, data: unknown) => void,
  ): Promise<T> {
    if (!path.startsWith("/api/")) throw new ApiError("不允许的接口地址");
    const startingSid = this.sessionId,
      startingVersion = this.version,
      hadUser = Boolean(this.user);
    if (!startingSid) throw new ApiError("请先登录", 401, "LOGIN_REQUIRED");
    await this.ensureSession();
    if (
      this.sessionId !== startingSid ||
      (hadUser && this.version !== startingVersion)
    ) {
      throw new ApiError("账号已切换，请重新操作", 0, "SESSION_CHANGED");
    }
    if (!this.accessToken)
      throw new ApiError("请先登录", 401, "LOGIN_REQUIRED");
    const version = this.version;
    const sentToken = this.accessToken;
    try {
      let response = await this.raw(
        path,
        init,
        this.sessionId,
        this.accessToken,
        true,
        Boolean(onFrame),
      );
      if (response.status === 401) {
        const errorBody = await response
          .clone()
          .json()
          .catch(() => null);
        this.assertCurrent(version);
        if (errorBody?.error?.code === "ACCESS_EXPIRED") {
          if (sentToken === this.accessToken) await this.refresh();
          this.assertCurrent(version);
          response = await this.raw(
            path,
            init,
            this.sessionId,
            this.accessToken,
            true,
            Boolean(onFrame),
          );
        }
      }
      if (onFrame && response.ok) {
        await this.readStream(response, version, onFrame);
        return undefined as T;
      }
      return await this.read<T>(response, version);
    } catch (error) {
      if (
        onFrame &&
        version === this.version &&
        error instanceof ApiError &&
        error.code === "ACCESS_EXPIRED"
      ) {
        await this.refresh();
        this.assertCurrent(version);
        return undefined as T;
      }
      if (
        version === this.version &&
        error instanceof ApiError &&
        error.status === 401
      ) {
        const sid = this.sessionId;
        this.clear();
        if (sid) this.publish(sid);
      }
      throw error;
    }
  }

  async stream(
    path: string,
    onFrame: (event: string, data: unknown) => void,
    signal: AbortSignal,
  ): Promise<void> {
    await this.request<void>(
      path,
      { signal, headers: { Accept: "text/event-stream" } },
      onFrame,
    );
  }

  private async readStream(
    response: Response,
    version: number,
    onFrame: (event: string, data: unknown) => void,
  ) {
    this.assertCurrent(version);
    const sid = response.headers.get("X-Auth-Session");
    if (sid && sid !== this.sessionId)
      throw new ApiError("账号会话不匹配", 401, "SESSION_INVALID");
    if (
      !response.headers.get("Content-Type")?.startsWith("text/event-stream") ||
      !response.body
    )
      throw new ApiError("实时连接暂不可用");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let pending = "",
      event = "message",
      data: string[] = [],
      size = 0;
    try {
      while (true) {
        const { value, done } = await reader.read();
        this.assertCurrent(version);
        if (done) return;
        pending += decoder.decode(value, { stream: true });
        if (pending.length + size > 524288)
          throw new ApiError("实时响应过大，请重新连接");
        let newline;
        while ((newline = pending.indexOf("\n")) >= 0) {
          const line = pending.slice(0, newline).replace(/\r$/, "");
          pending = pending.slice(newline + 1);
          if (!line) {
            if (data.length) {
              this.assertCurrent(version);
              const parsed = JSON.parse(data.join("\n"));
              if (event === "error")
                throw new ApiError(
                  parsed.message || "实时连接中断",
                  parsed.status || 503,
                  parsed.code,
                );
              onFrame(event, parsed);
            }
            event = "message";
            data = [];
            size = 0;
          } else if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) {
            data.push(line.slice(5).trimStart());
            size += line.length;
          }
        }
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  async revalidate() {
    if (!this.user) return;
    const version = this.version;
    const user = await this.request<User>("/api/auth/me");
    this.assertCurrent(version);
    if (this.user?.role !== user.role) this.version++;
    this.user = user;
    this.changed();
  }

  async logout() {
    const sid = this.sessionId,
      token = this.accessToken;
    this.clear();
    if (!sid) return;
    const flight = (async () => {
      const response = await this.raw(
        "/api/auth/logout",
        { method: "POST", body: "{}" },
        sid,
        token,
        false,
      );
      if (!response.ok)
        throw new ApiError("本地已退出，服务端退出未确认，请重新尝试");
      this.publish(sid);
    })();
    this.logoutFlight = flight;
    try {
      await flight;
    } finally {
      if (this.logoutFlight === flight) this.logoutFlight = null;
    }
  }

  async changePassword(current: string, next: string) {
    const version = this.version;
    await this.request("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({
        current_password: current,
        new_password: next,
      }),
    });
    this.assertCurrent(version);
    const sid = this.sessionId;
    this.clear();
    if (sid) this.publish(sid);
  }
}
