import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { GitHubInboxView } from "./GitHubInboxView";
import type { GitHubInboxItem } from "../api";

const getGitHubInbox = vi.fn();
const actOnGitHubItem = vi.fn();
const pollGitHubNow = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getGitHubInbox: () => getGitHubInbox(),
    actOnGitHubItem: (...a: unknown[]) => actOnGitHubItem(...a),
    pollGitHubNow: () => pollGitHubNow(),
  };
});

function item(over: Partial<GitHubInboxItem>): GitHubInboxItem {
  return {
    key: "pr:1", kind: "dependabot_prs", repo: "proj", title: "bump lodash", url: "https://gh/pr/1", number: 1, author: "dependabot[bot]",
    summary: "dependabot: dep/1 -> main", state: "proposed", mode: "propose", reason: "policy: propose", task_id: null,
    created_at: Date.now() / 1000 - 120, updated_at: Date.now() / 1000 - 60, snoozed_until: null, ...over,
  };
}

describe("GitHubInboxView", () => {
  beforeEach(() => {
    getGitHubInbox.mockReset();
    actOnGitHubItem.mockReset();
    pollGitHubNow.mockReset();
  });

  it("shows proposed items with actions and hides dismissed ones by default", async () => {
    getGitHubInbox.mockResolvedValue({ items: [item({}), item({ key: "pr:2", title: "old one", state: "dismissed" })], last_poll: { at: Date.now() / 1000 - 300, results: [] } });
    const user = userEvent.setup();
    render(<GitHubInboxView isAdmin={true} />);
    expect(await screen.findByText(/#1 bump lodash/)).toBeInTheDocument();
    expect(screen.getByText(/1 item waiting for your decision/)).toBeInTheDocument();
    expect(screen.queryByText(/old one/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Approve & start task/ })).toBeInTheDocument();

    await user.click(screen.getByLabelText(/show dismissed/));
    expect(await screen.findByText(/old one/)).toBeInTheDocument();
  });

  it("approve calls the API and swaps the item for the returned one", async () => {
    getGitHubInbox.mockResolvedValue({ items: [item({})], last_poll: null });
    actOnGitHubItem.mockResolvedValue({ item: item({ state: "task_created", task_id: "abcdef123456", reason: "approved by operator" }), task_id: "abcdef123456" });
    const user = userEvent.setup();
    const onOpenTask = vi.fn();
    render(<GitHubInboxView isAdmin={false} onOpenTask={onOpenTask} />);
    await user.click(await screen.findByRole("button", { name: /Approve & start task/ }));
    await waitFor(() => expect(actOnGitHubItem).toHaveBeenCalledWith("proj", "pr:1", "approve", undefined));
    expect(await screen.findByText(/task started/)).toBeInTheDocument();
    expect(screen.getByText(/Task abcdef12 started on proj/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Open task abcdef12/ }));
    expect(onOpenTask).toHaveBeenCalledWith("abcdef123456", "proj");
    expect(screen.queryByRole("button", { name: /Approve & start task/ })).not.toBeInTheDocument();
  });

  it("snooze and dismiss send their actions; poll now is admin-only", async () => {
    getGitHubInbox.mockResolvedValue({ items: [item({})], last_poll: null });
    actOnGitHubItem.mockImplementation(async (_r: string, _k: string, action: string) => ({ item: item({ state: action === "snooze" ? "snoozed" : "dismissed" }), task_id: null }));
    const user = userEvent.setup();
    const { rerender } = render(<GitHubInboxView isAdmin={false} />);
    await user.click(await screen.findByRole("button", { name: "7 d" }));
    await waitFor(() => expect(actOnGitHubItem).toHaveBeenCalledWith("proj", "pr:1", "snooze", 7));
    expect(screen.queryByRole("button", { name: "Poll now" })).not.toBeInTheDocument();

    rerender(<GitHubInboxView isAdmin={true} />);
    expect(screen.getByRole("button", { name: "Poll now" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(actOnGitHubItem).toHaveBeenCalledWith("proj", "pr:1", "dismiss", undefined));
  });

  it("explains the empty state", async () => {
    getGitHubInbox.mockResolvedValue({ items: [], last_poll: null });
    render(<GitHubInboxView isAdmin={true} />);
    expect(await screen.findByText(/Nothing here that is active/)).toBeInTheDocument();
    expect(screen.getByText(/Settings → GitHub/)).toBeInTheDocument();
  });

  it("a finished task's item waits without a button, and one GitHub still reports comes back to start again", async () => {
    getGitHubInbox.mockResolvedValue({ items: [
      item({ key: "code:a", kind: "code_scanning", title: "[HIGH] js/path-injection", state: "awaiting_rescan",
             task_id: "78db77be1111", reason: "fix merged; waiting for GitHub to re-scan" }),
      item({ key: "code:b", kind: "code_scanning", title: "[HIGH] js/loop-bound-injection", state: "proposed",
             reason: "task 3320a0ee merged its fix, but GitHub still reports 1 alert after re-scanning",
             attempts: [{ task_id: "3320a0ee2222", finished_at: Date.now() / 1000 - 3600, still_open: [201] }] }),
    ], last_poll: null });
    const user = userEvent.setup();
    const onOpenTask = vi.fn();
    render(<GitHubInboxView isAdmin={true} onOpenTask={onOpenTask} />);
    expect(await screen.findByText(/fix merged, waiting for GitHub to re-scan/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Open task 78db77be/ })).toBeInTheDocument();
    // one start button only: the waiting item has none
    expect(screen.getAllByRole("button", { name: /start/i })).toHaveLength(1);
    expect(screen.getByRole("button", { name: /Start another task/ })).toBeInTheDocument();
    expect(screen.getByText(/GitHub still reported #201/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /task 3320a0ee/ }));
    expect(onOpenTask).toHaveBeenCalledWith("3320a0ee2222", "proj");
  });
});
