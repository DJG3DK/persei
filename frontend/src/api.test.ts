import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthError, deletePasskey, getBenchmarks, getMe, listPasskeys, listTasks, login, logout, passkeyLoginOptions, setAuthFailureHandler, unsubscribePush, verify2FA } from "./api";
import { response, user } from "./test/fixtures";

// api.ts carries several invariants that its own comments record as having
// been broken before. Each of those is a test here, because a comment does
// not fail a build.

function stubFetch(impl: (url: string, init?: RequestInit) => Promise<Response>) {
  const spy = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    impl(String(input), init),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

beforeEach(() => {
  setAuthFailureHandler(null);
});

describe("apiFetch — authenticated requests", () => {
  it("fires the auth-failure handler and throws AuthError on 401", async () => {
    stubFetch(async () => response({ detail: "nope" }, { status: 401 }));
    const onFail = vi.fn();
    setAuthFailureHandler(onFail);

    await expect(listTasks()).rejects.toBeInstanceOf(AuthError);
    expect(onFail).toHaveBeenCalledTimes(1);
  });

  it("does not fire the handler for non-401 failures", async () => {
    // A 500 is a broken server, not an expired session. Logging the operator
    // out on one would lose their place for an unrelated fault.
    stubFetch(async () => response({}, { status: 500 }));
    const onFail = vi.fn();
    setAuthFailureHandler(onFail);

    await expect(listTasks()).rejects.toBeTruthy();
    expect(onFail).not.toHaveBeenCalled();
  });

  it("survives having no handler registered", async () => {
    stubFetch(async () => response({}, { status: 401 }));
    await expect(listTasks()).rejects.toBeInstanceOf(AuthError);
  });

  it("calls global fetch exactly once — it must not recurse into itself", async () => {
    // Guards the bug named in api.ts: apiFetch once called apiFetch instead
    // of fetch, stack-overflowing every authenticated call.
    const spy = stubFetch(async () => response([]));
    await listTasks();
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("reports a timeout as a readable error, not a raw DOMException", async () => {
    stubFetch(async () => {
      throw new DOMException("timed out", "TimeoutError");
    });
    await expect(listTasks()).rejects.toThrow(/timed out after \d+s/);
  });

  it.each([
    ["listPasskeys", () => listPasskeys()],
    ["deletePasskey", () => deletePasskey(1)],
  ])("%s is a signed-in call: a 401 is an expired session (audit U26)", async (_name, call) => {
    const spy = stubFetch(async () => response({ detail: "nope" }, { status: 401 }));
    const onFail = vi.fn();
    setAuthFailureHandler(onFail);

    await expect(call()).rejects.toBeInstanceOf(AuthError);
    expect(onFail).toHaveBeenCalledTimes(1);
    expect(spy.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal);
  });

  it("passes other network errors through untouched", async () => {
    stubFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    await expect(listTasks()).rejects.toThrow("Failed to fetch");
  });
});

describe("pre-auth endpoints", () => {
  // These deliberately bypass apiFetch: a 401 from them means "bad
  // credentials", and treating it as an expired session would log the user
  // out of the login screen.
  it("login does not trigger the auth-failure handler on 401", async () => {
    stubFetch(async () => response({ detail: "bad credentials" }, { status: 401 }));
    const onFail = vi.fn();
    setAuthFailureHandler(onFail);

    await expect(login("a@b.c", "wrong")).rejects.toThrow("bad credentials");
    expect(onFail).not.toHaveBeenCalled();
  });

  it("verify2FA does not trigger the auth-failure handler on 401", async () => {
    stubFetch(async () => response({ detail: "invalid code" }, { status: 401 }));
    const onFail = vi.fn();
    setAuthFailureHandler(onFail);

    await expect(verify2FA("tmp", "000000")).rejects.toThrow("invalid code");
    expect(onFail).not.toHaveBeenCalled();
  });

  it("passkey sign-in does not trigger the auth-failure handler on 401", async () => {
    stubFetch(async () => response({ detail: "no" }, { status: 401 }));
    const onFail = vi.fn();
    setAuthFailureHandler(onFail);

    await expect(passkeyLoginOptions()).rejects.toThrow("no");
    expect(onFail).not.toHaveBeenCalled();
  });

  it("surfaces the server's detail message rather than a status code", async () => {
    stubFetch(async () => response({ detail: "account locked" }, { status: 403 }));
    await expect(login("a@b.c", "x")).rejects.toThrow("account locked");
  });

  it("falls back to a status message when the body is not JSON", async () => {
    stubFetch(async () => ({
      ok: false,
      status: 502,
      json: async () => {
        throw new Error("not json");
      },
    }) as unknown as Response);
    await expect(login("a@b.c", "x")).rejects.toThrow(/502/);
  });

  it("returns the 2FA challenge without a user when one is required", async () => {
    stubFetch(async () => response({ requires_2fa: true, temp_token: "tmp-1" }));
    await expect(login("a@b.c", "pw")).resolves.toEqual({
      requires_2fa: true,
      temp_token: "tmp-1",
    });
  });
});

describe("session", () => {
  it("getMe returns the current user", async () => {
    const me = user({ email: "operator@example.test" });
    stubFetch(async () => response(me));
    await expect(getMe()).resolves.toMatchObject({ email: "operator@example.test" });
  });

  it("logout resolves even when the server rejects it", async () => {
    // The client-side session is gone either way; surfacing an error here
    // would strand someone on a screen they have already left.
    stubFetch(async () => response({}, { status: 500 }));
    await expect(logout()).resolves.not.toThrow();
  });
});

// The backend serves the SPA from a catch-all, so a route the running process
// does not have answers 200 with index.html rather than 404. A frontend
// deployed ahead of its backend hits that on purpose here (a restart kills
// whatever task is in flight), and "Unexpected token '<'" is not a useful
// thing for the dashboard to say about it.
describe("getBenchmarks — telling a missing route from a real answer", () => {
  function res(body: unknown, contentType: string, status = 200): Response {
    return {
      ok: status >= 200 && status < 300,
      status,
      headers: { get: (k: string) => (k.toLowerCase() === "content-type" ? contentType : null) },
      json: async () => body,
      text: async () => String(body),
    } as unknown as Response;
  }

  it("reports route-missing when the catch-all returns the SPA", async () => {
    stubFetch(async () => res("<!doctype html>", "text/html; charset=utf-8"));
    await expect(getBenchmarks()).rejects.toThrow("route-missing");
  });

  it("reports route-missing on a real 404 too", async () => {
    stubFetch(async () => res({ detail: "nope" }, "application/json", 404));
    await expect(getBenchmarks()).rejects.toThrow("route-missing");
  });

  it("surfaces a genuine server error as itself, not as a missing route", async () => {
    stubFetch(async () => res({ detail: "boom" }, "application/json", 500));
    await expect(getBenchmarks()).rejects.toThrow("getBenchmarks failed: 500");
  });

  it("returns the body when the route is there", async () => {
    stubFetch(async () => res({ window_days: 14 }, "application/json"));
    await expect(getBenchmarks(14)).resolves.toEqual({ window_days: 14 });
  });

  it("passes the window through as a query parameter", async () => {
    const spy = stubFetch(async () => res({ window_days: 7 }, "application/json"));
    await getBenchmarks(7);
    expect(spy.mock.calls[0][0]).toContain("window_days=7");
  });
});

describe("unsubscribePush", () => {
  it("sends the subscription's auth secret, which the server requires for a row it does not own", async () => {
    const spy = stubFetch(async () => response({ subscriptions: 0 }));
    await unsubscribePush("https://push.example/abc", "secret-auth");
    expect(JSON.parse(String(spy.mock.calls[0][1]?.body))).toEqual({
      endpoint: "https://push.example/abc",
      auth: "secret-auth",
    });
  });
});
