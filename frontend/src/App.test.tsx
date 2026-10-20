import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { session, task, user } from "./test/fixtures";

// The gate order in App is: loading -> landing/login -> forced password
// change -> forced TOTP enrolment -> the app. Each branch is a real state an
// operator can land in, and getting the order wrong locks someone out.

const getMe = vi.fn();
const listTasks = vi.fn<() => Promise<import("./types").TaskMeta[]>>(async () => []);
const listPlanningSessions = vi.fn(async () => []);
const listRepos = vi.fn(async () => ["webapp"]);
const logout = vi.fn(async () => {});
const getGitHubSettings = vi.fn();
const createProject = vi.fn();
const createPlanningSession = vi.fn();
// The session view hydrates on mount; without this the bare fetch stub's
// `{}` has no `meta` and the hook throws inside a state update.
const getPlanningSession = vi.fn(async (id: string) => ({
  meta: session({ session_id: id, repo: "my-app", title: null, plan_markdown: null }),
  log: [],
  running: false,
}));
let authFailureHandler: (() => void) | null = null;

// Spread the real module and override only what this file drives. Listing
// exports by hand meant any newly-added API function broke every test here
// with "No X export is defined on the mock" -- a failure about the mock, not
// about the app.
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    getMe: () => getMe(),
    listTasks: () => listTasks(),
    listPlanningSessions: () => listPlanningSessions(),
    listRepos: () => listRepos(),
    logout: () => logout(),
    getGitHubSettings: () => getGitHubSettings(),
    createProject: (...a: unknown[]) => createProject(...a),
    createPlanningSession: (...a: unknown[]) => createPlanningSession(...a),
    getPlanningSession: (id: string) => getPlanningSession(id),
    // The task view's stream hydrates from this; the bare fetch stub's `{}`
    // has no `meta`.
    getTask: async (id: string) => ({ meta: task({ task_id: id, status: "running" }), state: null, orphaned: false, seq: 0 }),
    setAuthFailureHandler: (fn: (() => void) | null) => {
      authFailureHandler = fn;
    },
  };
});

import App from "./App";

beforeEach(() => {
  authFailureHandler = null;
  getMe.mockReset();
  listTasks.mockReset().mockResolvedValue([]);
  listRepos.mockClear();
  getGitHubSettings.mockReset();
  getGitHubSettings.mockResolvedValue({ env_token: false, settings: { tokens: {} } });
  createProject.mockReset();
  createPlanningSession.mockReset();
  // Anything this file does not explicitly stub still reaches the real
  // wrapper, so give it a benign response rather than an unhandled rejection.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "{}" })),
  );
});

describe("App — signed out", () => {
  it("shows the sign-in form: this host is the console, nothing else", async () => {
    // Before the split there was a landing page in front of this, and the
    // opening getMe() 401 had to be told apart from a real expiry so a
    // visitor was not shoved past it. perseiagent.ai serves that page now, and
    // agent.perseiagent.ai is the console -- so signed out simply means sign in.
    getMe.mockRejectedValue(new Error("401"));
    render(<App />);
    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
  });

  it("carries no product pitch, only a link to where it lives", async () => {
    getMe.mockRejectedValue(new Error("401"));
    render(<App />);
    await screen.findByRole("heading", { name: /sign in/i });
    expect(document.body.textContent).not.toMatch(/autonomous coding agent/i);
    expect(screen.getByRole("link", { name: /what is persei/i }))
      .toHaveAttribute("href", "https://perseiagent.ai");
  });
});

describe("App — session expiry", () => {
  it("returns to the sign-in form", async () => {
    // Once the landing page moved off this host there is nothing else for an
    // expiry to land on, but the handler still has to clear the user.
    getMe.mockResolvedValue(user());
    render(<App />);
    await waitFor(() => expect(authFailureHandler).toBeTypeOf("function"));

    // Invoking the handler directly is a state update from outside React's
    // event system, so it needs wrapping the way a real 401 would not.
    await act(async () => authFailureHandler!());
    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  });
});

describe("App — forced flows, in order", () => {
  it("demands a password change before anything else", async () => {
    getMe.mockResolvedValue(user({ must_change_password: true, require_totp_setup: true }));
    render(<App />);
    expect(await screen.findByRole("heading", { name: /change|password/i })).toBeInTheDocument();
  });

  it("demands TOTP enrolment once the password is settled", async () => {
    getMe.mockResolvedValue(user({ must_change_password: false, require_totp_setup: true }));
    render(<App />);
    expect(
      await screen.findByRole("heading", { name: /two-factor|authenticator|2fa/i }),
    ).toBeInTheDocument();
  });

  it("renders the console for a fully set-up user", async () => {
    getMe.mockResolvedValue(user());
    render(<App />);
    expect(await screen.findByRole("button", { name: /new plan/i })).toBeInTheDocument();
  });
});

describe("App — inside the desktop app", () => {
  // The desktop window is frameless: this strip is its only drag handle and
  // its only close button. It rendered inside AuthenticatedApp, so the
  // sign-in, change-password and 2FA-setup screens had neither (2026-09-29
  // audit, U2).
  const inApp = () => {
    const win = { minimize: async () => {}, toggleMaximize: async () => {}, close: async () => {} };
    (window as unknown as { __TAURI__: unknown }).__TAURI__ = { window: { getCurrentWindow: () => win } };
  };
  afterEach(() => {
    delete (window as unknown as { __TAURI__?: unknown }).__TAURI__;
    document.documentElement.classList.remove("in-desktop-app");
  });

  it("the sign-in screen has the title bar", async () => {
    inApp();
    getMe.mockRejectedValue(new Error("401"));
    render(<App />);
    await screen.findByRole("heading", { name: /sign in/i });
    expect(screen.getByTestId("titlebar")).toBeInTheDocument();
  });

  it("the forced password change has the title bar", async () => {
    inApp();
    getMe.mockResolvedValue(user({ must_change_password: true }));
    render(<App />);
    await screen.findByRole("heading", { name: /change|password/i });
    expect(screen.getByTestId("titlebar")).toBeInTheDocument();
  });

  it("the forced 2FA enrolment has the title bar", async () => {
    inApp();
    getMe.mockResolvedValue(user({ require_totp_setup: true }));
    render(<App />);
    await screen.findByRole("heading", { name: /two-factor|authenticator|2fa/i });
    expect(screen.getByTestId("titlebar")).toBeInTheDocument();
  });

  it("a browser tab has none", async () => {
    getMe.mockRejectedValue(new Error("401"));
    render(<App />);
    await screen.findByRole("heading", { name: /sign in/i });
    expect(screen.queryByTestId("titlebar")).not.toBeInTheDocument();
  });
});

describe("App — per-task state", () => {
  // Selecting a task pushes its URL; the next test expects to start on "/".
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("a draft typed for one task does not follow the operator to the next", async () => {
    // TaskView had no key, so React kept one instance across tasks and the
    // composer, the resume panel and the diff panel all carried their state
    // over (2026-09-29 audit, U3).
    const a = task({ task_id: "task-a", goal: "first task", status: "running" });
    const b = task({ task_id: "task-b", goal: "second task", status: "running" });
    getMe.mockResolvedValue(user());
    listTasks.mockResolvedValue([a, b]);
    render(<App />);
    await userEvent.click(await screen.findByText("first task"));
    const box = await screen.findByLabelText("Message the agent");
    await userEvent.type(box, "only for the first");
    expect(box).toHaveValue("only for the first");
    await userEvent.click(screen.getByText("second task"));
    expect(await screen.findByLabelText("Message the agent")).toHaveValue("");
  });
});

describe("App — initial load", () => {
  it("shows a loading state rather than flashing the landing page", async () => {
    // A slow auth check must be distinguishable from a crashed render.
    let settle: (v: unknown) => void = () => {};
    getMe.mockReturnValue(new Promise((r) => (settle = r)));
    render(<App />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
    settle(user());
    await waitFor(() => expect(screen.queryByText(/loading/i)).not.toBeInTheDocument());
  });
});

describe("App — the planner's New project door", () => {
  it("reloads the repo list after a project is created, before the session opens", async () => {
    // Repos used to be fetched once at mount and never again, so a project
    // created from the planner was invisible to every dropdown until a
    // page reload.
    getMe.mockResolvedValue(user());
    createProject.mockResolvedValue({
      ok: true, name: "my-app", live: "/srv/projects/my-app", github: null,
      steps: [{ step: "init", ok: true, detail: "git init" }],
    });
    createPlanningSession.mockResolvedValue({ session_id: "s1", repo: "my-app" });
    render(<App />);
    await screen.findByRole("button", { name: /new plan/i });
    await waitFor(() => expect(listRepos).toHaveBeenCalledTimes(1));

    await userEvent.selectOptions(screen.getByLabelText(/^repo$/i), "__new__");
    await userEvent.type(screen.getByLabelText(/project name/i), "my-app");
    await userEvent.click(screen.getByRole("button", { name: /create & start/i }));

    await waitFor(() => expect(createPlanningSession).toHaveBeenCalledWith("my-app", "auto"));
    expect(listRepos).toHaveBeenCalledTimes(2);
    expect(listRepos.mock.invocationCallOrder[1]).toBeGreaterThan(createProject.mock.invocationCallOrder[0]);
    expect(listRepos.mock.invocationCallOrder[1]).toBeLessThan(createPlanningSession.mock.invocationCallOrder[0]);
  });

  it("probes GitHub settings for an admin and offers the private-repo box when a token exists", async () => {
    getMe.mockResolvedValue(user());
    getGitHubSettings.mockResolvedValue({ env_token: false, settings: { tokens: { main: { hint: "ghp_…", created_at: null } } } });
    render(<App />);
    await screen.findByRole("button", { name: /new plan/i });
    await userEvent.selectOptions(screen.getByLabelText(/^repo$/i), "__new__");
    expect(await screen.findByRole("checkbox", { name: /private github repo/i })).toBeChecked();
  });

  it("never probes GitHub settings for a non-admin", async () => {
    // The endpoint is admin-only; a restricted account would only collect a
    // 403 for a checkbox it cannot see.
    getMe.mockResolvedValue(user({ role: "user" }));
    render(<App />);
    await screen.findByRole("button", { name: /new plan/i });
    await waitFor(() => expect(listRepos).toHaveBeenCalled());
    expect(getGitHubSettings).not.toHaveBeenCalled();
    expect(screen.queryByRole("option", { name: /new project/i })).not.toBeInTheDocument();
  });
});
