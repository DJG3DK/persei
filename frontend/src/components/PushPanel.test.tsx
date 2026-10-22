import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PushPanel } from "./PushPanel";
import { serviceWorkerReady } from "../registerServiceWorker";

const getPushKey = vi.fn();

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  getPushKey: (...a: unknown[]) => getPushKey(...a),
}));

/* Audit U29 (2026-10-22): `serviceWorker.ready` never settles on a page no
 * worker registered (`vite dev`, a refused registration), and the Enable
 * button sat on "Enabling…" for good. */
function fakeWorkers(registration: unknown, ready: Promise<unknown>) {
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { getRegistration: vi.fn(async () => registration), ready },
  });
}

beforeEach(() => {
  getPushKey.mockReset().mockResolvedValue({ public_key: "AQID", subscriptions: 0 });
  vi.stubGlobal("PushManager", function PushManager() {});
  vi.stubGlobal("Notification", { permission: "default", requestPermission: vi.fn(async () => "granted") });
});

afterEach(() => {
  vi.useRealTimers();
  Reflect.deleteProperty(navigator, "serviceWorker");
});

describe("PushPanel with no service worker", () => {
  it("says so instead of hanging on Enabling…", async () => {
    fakeWorkers(undefined, new Promise(() => undefined));
    render(<PushPanel />);
    await userEvent.click(await screen.findByRole("button", { name: /enable on this device/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/no service worker is running/i);
    expect(screen.getByRole("button", { name: /enable on this device/i })).toBeEnabled();
  });
});

describe("serviceWorkerReady", () => {
  it("gives up on a worker that never activates", async () => {
    vi.useFakeTimers();
    fakeWorkers({}, new Promise(() => undefined));
    const pending = serviceWorkerReady(5_000);
    const check = expect(pending).rejects.toThrow(/did not start/);
    await vi.advanceTimersByTimeAsync(5_000);
    await check;
  });

  it("returns the registration once it is active", async () => {
    const reg = { pushManager: {} };
    fakeWorkers(reg, Promise.resolve(reg));
    await expect(serviceWorkerReady()).resolves.toBe(reg);
  });
});
