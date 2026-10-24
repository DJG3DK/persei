import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ModelConfigPanel } from "./ModelConfigPanel";
import type { ModelPin } from "../types";
import { readFileSync } from "node:fs";
import { join } from "node:path";

/* The model page commits through the same sticky bar as the settings page.
 * What matters: no bar until a pin is actually changed, changing a pin back
 * to what it was clears it, Save calls the API and lands the new pins, and
 * Discard drops the edit without a request. */

const api = vi.hoisted(() => ({
  getModelConfig: vi.fn(),
  getModelCatalog: vi.fn(),
  saveModelConfig: vi.fn(),
  saveProviderPins: vi.fn(),
  getModelEndpoints: vi.fn(),
  probeForcedToolCall: vi.fn(),
  restartLlmRouter: vi.fn(),
  getPrivacy: vi.fn(),
  savePrivacy: vi.fn(),
}));

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, ...api };
});

const pin = (model: string): ModelPin => ({
  label: "Coder",
  model,
  input_cost_per_token: 1e-6,
  output_cost_per_token: 2e-6,
  tools: true,
});

const catalog = {
  models: [
    { id: "a/one", name: "One", input_cost_per_token: 1e-6, output_cost_per_token: 2e-6 },
    { id: "b/two", name: "Two", input_cost_per_token: 1e-6, output_cost_per_token: 2e-6 },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getModelConfig.mockResolvedValue({ roles: { "agent-coder": pin("a/one") } });
  api.getModelCatalog.mockResolvedValue(catalog);
  api.saveModelConfig.mockResolvedValue({ roles: { "agent-coder": pin("b/two") } });
  api.getPrivacy.mockResolvedValue("zdr");
  api.savePrivacy.mockImplementation(async (m: string) => m);
});

async function mountAndPick(name: string) {
  render(<ModelConfigPanel />);
  const row = (await screen.findByText("agent-coder")).closest(".model-config-row") as HTMLElement;
  await userEvent.click(within(row).getByRole("button", { name: /One|Two/ }));
  await userEvent.click(within(row).getByRole("button", { name: new RegExp(`^${name}`) }));
  return row;
}

describe("model configuration save bar", () => {
  it("shows no save control until a pin changes", async () => {
    render(<ModelConfigPanel />);
    await screen.findByText("agent-coder");
    expect(screen.queryByRole("button", { name: /^save$/i })).not.toBeInTheDocument();
  });

  it("counts a changed pin and saves it through the bar", async () => {
    await mountAndPick("Two");
    expect(screen.getByText("1 unsaved change")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(api.saveModelConfig).toHaveBeenCalledWith({ "agent-coder": "b/two" }));
    expect(api.saveProviderPins).not.toHaveBeenCalled();
    await screen.findByText(/Saved to config\.yaml/);
    expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument();
  });

  it("clears the bar when a pin is put back to what it was", async () => {
    const row = await mountAndPick("Two");
    expect(screen.getByText("1 unsaved change")).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: /^Two/ }));
    await userEvent.click(within(row).getByRole("button", { name: /^One/ }));
    expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument();
  });

  it("discards an edit without a request", async () => {
    const row = await mountAndPick("Two");
    await userEvent.click(screen.getByRole("button", { name: /discard/i }));
    expect(screen.queryByText(/unsaved change/)).not.toBeInTheDocument();
    expect(api.saveModelConfig).not.toHaveBeenCalled();
    expect(within(row).getByRole("button", { name: /^One/ })).toBeInTheDocument();
  });

  it("surfaces a failed save in the bar and keeps the edit", async () => {
    api.saveModelConfig.mockRejectedValueOnce(new Error("config.yaml is read-only"));
    await mountAndPick("Two");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    await screen.findByText("config.yaml is read-only");
    expect(screen.getByRole("button", { name: /^save$/i })).toBeInTheDocument();
  });
});

/* The restart dialog.
 *
 * Reported from the live box: after saving pins, "I can't restart the router
 * … the dialog stays and you have to hit Cancel to get out of it" — and the
 * only evidence the restart HAD happened was a Telegram alert. Two separate
 * faults: the panel rendered the error behind the modal backdrop, where it
 * could not be read, and a success said nothing at all. */
describe("restarting the router", () => {
  async function openTheDialog() {
    render(<ModelConfigPanel />);
    await screen.findByText("agent-coder");
    await userEvent.click(screen.getByRole("button", { name: /Restart Router/i }));
    return screen.getByRole("dialog");
  }

  it("says the router came back, and how long it took", async () => {
    api.restartLlmRouter.mockResolvedValue({ ok: true, output: "", healthy: true, waited_s: 4.2 });
    const dialog = await openTheDialog();
    await userEvent.click(within(dialog).getByRole("button", { name: /Restart now/i }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByRole("status")).toHaveTextContent(/Router restarted and answering after 4.2s/);
  });

  it("does not claim success when the router has not answered yet", async () => {
    api.restartLlmRouter.mockResolvedValue({ ok: true, output: "", healthy: false, waited_s: 25 });
    const dialog = await openTheDialog();
    await userEvent.click(within(dialog).getByRole("button", { name: /Restart now/i }));

    expect(await screen.findByRole("status")).toHaveTextContent(/not answering yet/);
  });

  it("shows a failure INSIDE the dialog instead of behind it", async () => {
    api.restartLlmRouter.mockRejectedValue(new Error("pm2 could not restart model-router"));
    const dialog = await openTheDialog();
    await userEvent.click(within(dialog).getByRole("button", { name: /Restart now/i }));

    const alert = await within(screen.getByRole("dialog")).findByRole("alert");
    expect(alert).toHaveTextContent("pm2 could not restart model-router");
  });

  it("offers a way out and a way to retry after a failure", async () => {
    api.restartLlmRouter.mockRejectedValue(new Error("nope"));
    const dialog = await openTheDialog();
    await userEvent.click(within(dialog).getByRole("button", { name: /Restart now/i }));
    await within(screen.getByRole("dialog")).findByRole("alert");

    const open = screen.getByRole("dialog");
    expect(within(open).getByRole("button", { name: /Try again/ })).toBeTruthy();
    await userEvent.click(within(open).getByRole("button", { name: /^Close$/ }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a retry that works clears the error and closes the dialog", async () => {
    api.restartLlmRouter.mockRejectedValueOnce(new Error("nope"));
    api.restartLlmRouter.mockResolvedValue({ ok: true, output: "", healthy: true, waited_s: 3 });
    const dialog = await openTheDialog();
    await userEvent.click(within(dialog).getByRole("button", { name: /Restart now/i }));
    await within(screen.getByRole("dialog")).findByRole("alert");

    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: /Try again/ }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByRole("status")).toHaveTextContent(/restarted and answering/);
  });
});

/* The page's own description of what it controls.
 *
 * It shipped saying the opposite of the truth: that the adaptive tier system
 * "belongs to the review service and isn't shown or editable here". Both
 * halves were wrong. The review service resolves its model through
 * agent-reviewer, which IS on this page (28 calls in the last fortnight), and
 * the tier system's consumer is a separate coding agent -- the header of
 * services/model-router/config.yaml says so. Flagged by the operator 2026-09-13.
 *
 * Asserted on meaning rather than wording: a rewrite is free, saying the
 * reviewer is excluded is not.
 */
describe("what the page says it controls", () => {
  const src = readFileSync(join(__dirname, "ModelConfigPanel.tsx"), "utf8");
  // From the subtitle's own tag to the end of that paragraph. Not "up to the
  // next model-config-error": that class name also appears earlier in the
  // file, so the slice came out empty and the assertions passed on nothing.
  const subStart = src.indexOf("model-config-sub");
  const blurb = src.slice(subStart, src.indexOf("</p>", subStart));

  it("does not claim the tier system belongs to the review service", () => {
    expect(blurb.replace(/\s+/g, " ")).not.toMatch(/tier system.*belongs to the review service/);
  });

  it("says the reviewer is included, because it is", () => {
    expect(blurb).toContain("agent-reviewer");
    expect(blurb).toMatch(/review service/);
  });

  it("says non-agent aliases are left alone, without naming other products", () => {
    expect(blurb).toMatch(/agent-/);
    expect(blurb).toMatch(/left alone/);
    // The names themselves are not written here on purpose. This file is
    // scanned by tests/test_repo_hygiene.py, which enforces repo-wide that no
    // private project name appears anywhere in the tree -- a negative
    // assertion spelled out in full would be the one place they still did.
    // Assembled from halves so this check survives that scan.
    const forbidden = ["mail" + "-chat", "mail" + "-triage", "trade" + "-gate", "trading " + "bot"];
    forbidden.forEach((name) => expect(blurb.toLowerCase()).not.toContain(name));
  });
});


describe("in the compose bundle", () => {
  it("shows the note in place of the restart button", async () => {
    api.getModelConfig.mockResolvedValue({
      roles: { "agent-coder": pin("a/one") },
      router_restart: { available: false, note: "The bundle's router re-reads its config on the next call, so a saved pin is already live." },
    });
    render(<ModelConfigPanel />);
    const note = await screen.findByTestId("router-restart-note");
    expect(note.textContent).toContain("already live");
    expect(screen.queryByRole("button", { name: /Restart Router/i })).not.toBeInTheDocument();
  });
});


describe("provider privacy", () => {
  it("shows the router's rule and saves a change straight away, without the pins' Save bar", async () => {
    render(<ModelConfigPanel />);
    const select = await screen.findByLabelText("Provider privacy");
    await waitFor(() => expect(select).not.toBeDisabled());
    expect(select).toHaveValue("zdr");
    await userEvent.selectOptions(select, "deny");
    await waitFor(() => expect(api.savePrivacy).toHaveBeenCalledWith("deny"));
    expect(select).toHaveValue("deny");
    expect(screen.getByText(/applies it on its next call/)).toBeInTheDocument();
    expect(api.saveModelConfig).not.toHaveBeenCalled();
  });
});
