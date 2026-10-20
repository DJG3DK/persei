import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { EvalRunSummary, EvalsOverview } from "../types";
import { EvalsPanel } from "./EvalsPanel";

const getEvals = vi.fn();
const getEvalRun = vi.fn();
const startEvalRun = vi.fn();
const stopEvalRun = vi.fn();
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getEvals: () => getEvals(),
    getEvalRun: (n: string) => getEvalRun(n),
    startEvalRun: (...a: unknown[]) => startEvalRun(...a),
    stopEvalRun: () => stopEvalRun(),
  };
});

const run = (name: string, over: Partial<EvalRunSummary> = {}): EvalRunSummary => ({
  name, started_at: name.replace(/T(\d\d)-(\d\d)-(\d\d)Z/, "T$1:$2:$3Z"), finished_at: null, duration_s: 3600,
  notes: "", tasks_total: 2, tasks_attempted: 2, tasks_passed: 2, pass_rate: 100, total_cost_usd: 0.1,
  stopped_early: false, full: true, by_category: { "bug-fix": { tasks: 2, passed: 2 } },
  benchmarks: { first_pass_rate: 100, escalation_rate: 0, escalated: 0, cost_median: 0.05 },
  failed: [], results: { a: true, b: true }, ...over,
});

const overview = (over: Partial<EvalsOverview> = {}): EvalsOverview => ({
  runs: [
    run("2026-09-23T12-00-00Z", { tasks_passed: 1, pass_rate: 50, failed: ["b"], results: { a: true, b: false } }),
    run("2026-09-22T12-00-00Z"),
  ],
  status: null,
  suite: { tasks: 2, by_category: { "bug-fix": 2 }, ids: ["a", "b"] },
  estimate: { cost_usd: 0.1, duration_s: 3600, from_run: "2026-09-23T12-00-00Z" },
  ...over,
});

const reportFor = (failing: string[]) => ({
  summary: {},
  tasks: ["a", "b"].map((id) => ({
    id, fixture: "pylib", category: "bug-fix", passed: !failing.includes(id), outcome: "shipped",
    escalation_reason: null, review_verdict: "READY", iterations: 0, cost_usd: 0.05, duration_s: 1800,
    changed_paths: ["src/x.py"], diff: failing.includes(id) ? "--- a/src/x.py\n+++ b/src/x.py" : undefined,
    assertions: [{ kind: "command", describe: "command: check", ok: !failing.includes(id), undetermined: false,
      detail: failing.includes(id) ? "exited 1" : "" }],
    error: "",
  })),
});

beforeEach(() => {
  getEvals.mockReset().mockResolvedValue(overview());
  getEvalRun.mockReset().mockResolvedValue(reportFor(["b"]));
  startEvalRun.mockReset().mockResolvedValue({ ok: true, tasks: 2 });
  stopEvalRun.mockReset().mockResolvedValue(undefined);
});

describe("EvalsPanel", () => {
  it("headlines the latest full run and flags what regressed since the one before", async () => {
    const { container } = render(<EvalsPanel />);
    await screen.findByText(/tasks passed/);
    expect(container.querySelector(".evals-score-big")).toHaveTextContent("1/2");
    expect(screen.getByText(/1 regressed/)).toBeInTheDocument();
    expect(await screen.findByText("regressed")).toBeInTheDocument();
  });

  it("a partial run never becomes the headline", async () => {
    getEvals.mockResolvedValue(overview({
      runs: [run("2026-09-24T12-00-00Z", { full: false, tasks_attempted: 1, tasks_passed: 0 }),
        run("2026-09-22T12-00-00Z")],
    }));
    const { container } = render(<EvalsPanel />);
    await screen.findByText(/tasks passed/);
    expect(container.querySelector(".evals-score-big")).toHaveTextContent("2/2");
    expect(screen.getByText("partial")).toBeInTheDocument();
  });

  it("a failed task opens to its failing assertion and diff", async () => {
    const { container } = render(<EvalsPanel />);
    await waitFor(() => expect(container.querySelector(".evals-task.is-fail .evals-task-row")).not.toBeNull());
    await userEvent.click(container.querySelector(".evals-task.is-fail .evals-task-row") as HTMLElement);
    expect(await screen.findByText(/exited 1/)).toBeInTheDocument();
    expect(screen.getByText(/\+\+\+ b\/src\/x\.py/)).toBeInTheDocument();
  });

  it("starting a run asks first, shows the estimate, and sends the note", async () => {
    render(<EvalsPanel />);
    await userEvent.click(await screen.findByRole("button", { name: /run the golden suite/i }));
    expect(screen.getByRole("dialog")).toHaveTextContent("$0.10");
    await userEvent.type(screen.getByPlaceholderText(/what changed/i), "new prompt");
    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    expect(startEvalRun).toHaveBeenCalledWith("new prompt", undefined, 1);
  });

  it("can run several tasks at once", async () => {
    render(<EvalsPanel />);
    await userEvent.click(await screen.findByRole("button", { name: /run the golden suite/i }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: /tasks at once/i }), "3");
    await userEvent.click(screen.getByRole("button", { name: /start run/i }));
    expect(startEvalRun).toHaveBeenCalledWith("", undefined, 3);
  });

  it("a running suite shows its progress and can be stopped", async () => {
    getEvals.mockResolvedValue(overview({
      status: { running: true, pid: 1, started_at: Date.now() / 1000 - 600, finished_at: null, notes: "", only: [],
        tasks_total: 2, done: 1, passed: 1, spent_usd: 0.05, exit_code: null, report: null,
        results: [{ id: "a", passed: true, cost_usd: 0.05, outcome: "shipped" }] },
    }));
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<EvalsPanel />);
    expect(await screen.findByRole("status")).toHaveTextContent(/1 of 2 done, 1 passed/);
    expect(screen.queryByRole("button", { name: /run the golden suite/i })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /^stop$/i }));
    await waitFor(() => expect(stopEvalRun).toHaveBeenCalled());
  });

  it("copies a scorecard paragraph", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<EvalsPanel />);
    await userEvent.click(await screen.findByRole("button", { name: /copy scorecard/i }));
    expect(writeText.mock.calls[0][0]).toMatch(/^Persei golden suite, 2026-09-23: 1\/2 tasks passed \(50%\)/);
  });
});
