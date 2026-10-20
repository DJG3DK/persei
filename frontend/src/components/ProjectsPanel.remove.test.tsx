/**
 * Removing a project has to be findable.
 *
 * It was reachable only by expanding a project row first, with nothing on the
 * row saying it was in there -- so the only way anyone found it was by
 * already knowing. Someone who had just added the wrong repository could see
 * it sitting in the list with no way to take it back off.
 *
 * And the second half: a repository Persei cloned by itself leaves a clone
 * behind that removal used to have no way to delete, so finishing the job
 * meant a shell.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectsPanel } from "./ProjectsPanel";

const listProjectsConfig = vi.fn();
const listProjectArchives = vi.fn();
const getDeployKey = vi.fn();
const checkoutRemovable = vi.fn();
const removeProject = vi.fn();

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listProjectsConfig: () => listProjectsConfig(),
    listProjectArchives: () => listProjectArchives(),
    getDeployKey: (...a: unknown[]) => getDeployKey(...a),
    checkoutRemovable: (...a: unknown[]) => checkoutRemovable(...a),
    removeProject: (...a: unknown[]) => removeProject(...a),
  };
});

beforeEach(() => {
  vi.clearAllMocks();
  listProjectsConfig.mockResolvedValue({
    projects: { brochure: { live: "/home/brochure" } },
    config_path: "/p/projects.json",
    restart_required_hint: "",
  });
  listProjectArchives.mockResolvedValue({ archives: [] });
  getDeployKey.mockResolvedValue({ project: "brochure", has_key: false });
  checkoutRemovable.mockResolvedValue({
    live: "/home/brochure", removable: true,
    reason: "every commit is on a remote and nothing is uncommitted in /home/brochure",
  });
});

async function openRemoval() {
  const user = userEvent.setup();
  render(<ProjectsPanel />);
  const row = (await screen.findByText("brochure")).closest("li")!;
  // The point of the test: reachable from the row, without expanding first.
  await user.click(within(row).getByRole("button", { name: /Remove brochure/ }));
  return { user, row };
}

describe("taking a project back off", () => {
  it("offers Remove on the row itself, not only once it is expanded", async () => {
    render(<ProjectsPanel />);
    const row = (await screen.findByText("brochure")).closest("li")!;
    expect(within(row).getByRole("button", { name: /Remove brochure/ })).toBeInTheDocument();
    // and nothing had to be expanded for that
    expect(within(row).getByRole("button", { expanded: false })).toBeInTheDocument();
  });

  it("opens the confirmation straight from that button", async () => {
    await openRemoval();
    expect(await screen.findByLabelText(/Type brochure to confirm/)).toBeInTheDocument();
  });

  it("offers to delete the checkout when nothing would be lost, and says why", async () => {
    const { user } = await openRemoval();
    const choice = await screen.findByRole("radio", { name: /Delete it from this machine/ });
    expect(choice).not.toBeDisabled();

    await user.click(choice);
    await user.type(screen.getByLabelText(/Type brochure to confirm/), "brochure");
    removeProject.mockResolvedValue({
      ok: true, name: "brochure", steps: [], archive: "a.json",
      live_untouched: null, live_removed: "/home/brochure",
    });
    await user.click(screen.getByRole("button", { name: /Remove and delete the checkout/ }));

    await waitFor(() => expect(removeProject)
      .toHaveBeenCalledWith("brochure", "archive", "delete"));
    // and the outcome survives the project leaving the list
    expect(await screen.findByText(/was deleted; everything in it was on its remote/))
      .toBeInTheDocument();
  });

  it("refuses the choice with the reason when something would be lost", async () => {
    checkoutRemovable.mockResolvedValue({
      live: "/home/brochure", removable: false,
      reason: "it has 2 commits that are not on any remote",
    });
    await openRemoval();
    const choice = await screen.findByRole("radio", { name: /Delete it from this machine/ });
    expect(choice).toBeDisabled();
    expect(screen.getByText(/not on any remote/)).toBeInTheDocument();
  });

  it("keeps the checkout by default", async () => {
    const { user } = await openRemoval();
    await screen.findByRole("radio", { name: /Leave it where it is/ });
    expect(screen.getByRole("radio", { name: /Leave it where it is/ })).toBeChecked();

    await user.type(screen.getByLabelText(/Type brochure to confirm/), "brochure");
    removeProject.mockResolvedValue({
      ok: true, name: "brochure", steps: [], archive: "a.json",
      live_untouched: "/home/brochure", live_removed: null,
    });
    await user.click(screen.getByRole("button", { name: /Remove and archive/ }));

    await waitFor(() => expect(removeProject)
      .toHaveBeenCalledWith("brochure", "archive", "keep"));
    expect(await screen.findByText(/was not touched/)).toBeInTheDocument();
  });
});
