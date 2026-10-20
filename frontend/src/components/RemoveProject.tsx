import { useEffect, useState } from "react";
import { removeProject, checkoutRemovable, type RemoveProjectResult, type CheckoutRemovable } from "../api";
import "./RemoveProject.css";

/* Taking a project off the agent.
 *
 * The confirmation asks the operator to type the project's name. That is not
 * ceremony: this control sits in a list of projects, one row apart from the
 * next one, and the cost of hitting the wrong row is an archive-or-delete of
 * everything the agent learned about something that was working fine.
 *
 * The panel leads with what is NOT affected, because that is the question
 * anyone hovering over a red button in a list of their repositories is
 * actually asking. The live repo, its branches and its remote are untouched
 * unless the operator explicitly asks for them to go, which is only offered
 * when the server can show that nothing would be lost by it.
 *
 * Open/closed is the parent's state: the button that opens this sits on the
 * project row, above the panel, so the panel cannot own it. The OUTCOME goes
 * to the parent for the same reason: a removed project leaves the list, this
 * panel goes with it, and a summary rendered in here would be unmounted
 * before anybody read it.
 */
export function RemoveProject({ name, live, open, onOpenChange, onRemoved }: {
  name: string;
  live: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRemoved: (result: RemoveProjectResult) => void | Promise<void>;
}) {
  const [memory, setMemory] = useState<"archive" | "delete">("archive");
  const [files, setFiles] = useState<"keep" | "delete">("keep");
  /* Whether the checkout could be deleted, and why not when it could not.
     Asked for when the panel opens rather than with the project list: it is
     several git commands per project and nobody needs the answer until they
     are standing in front of the choice. */
  const [checkout, setCheckout] = useState<CheckoutRemovable | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    // oxlint-disable-next-line react/set-state-in-effect -- the previous answer is cleared before asking again; opening is the external event
    setCheckout(null);
    let cancelled = false;
    checkoutRemovable(name)
      .then((r) => !cancelled && setCheckout(r))
      .catch(() => !cancelled && setCheckout({
        live, removable: false, reason: "could not check what is in the checkout",
      }));
    return () => { cancelled = true; };
  }, [open, name, live]);

  function reset() {
    onOpenChange(false);
    setTyped("");
    setError(null);
    setMemory("archive");
    setFiles("keep");
  }

  async function go() {
    setBusy(true);
    setError(null);
    try {
      const res = await removeProject(name, memory, files);
      onOpenChange(false);
      setTyped("");
      await onRemoved(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not remove the project");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <div className="rmproj">
        <button type="button" className="rmproj-open" onClick={() => onOpenChange(true)}>
          Remove from Persei
        </button>
        <span className="rmproj-hint">The repository itself is not affected.</span>
      </div>
    );
  }

  return (
    <div className="rmproj-confirm">
      <p className="rmproj-lead">
        Persei will stop watching <strong>{name}</strong>: its workspace, its deploy key and its
        configuration go away.
        {files === "keep"
          ? <> <code>{live}</code> is <strong>not</strong> touched — no files, no branches, no remote.</>
          : <> <code>{live}</code> will be <strong>deleted</strong> from this machine.</>}
      </p>

      <fieldset className="rmproj-choice">
        <legend>What the agent learned about it</legend>
        <label className={memory === "archive" ? "is-chosen" : ""}>
          <input
            type="radio"
            name={`memory-${name}`}
            checked={memory === "archive"}
            onChange={() => setMemory("archive")}
          />
          <span>
            <strong>Archive it</strong>
            <em>
              Its memory, generated skills, planning sessions and task history are saved to a
              file. Adding {name} again later offers to restore them, so it picks up where it
              left off.
            </em>
          </span>
        </label>
        <label className={memory === "delete" ? "is-chosen" : ""}>
          <input
            type="radio"
            name={`memory-${name}`}
            checked={memory === "delete"}
            onChange={() => setMemory("delete")}
          />
          <span>
            <strong>Delete it</strong>
            <em>Removed outright. There is nothing to restore from afterwards.</em>
          </span>
        </label>
      </fieldset>

      {/* Offered only when the server can show that nothing would be lost:
          every commit on a remote, nothing uncommitted or stashed, no
          declared secret file in the directory, and nothing this box runs
          out of it. Anything else and the reason is shown instead of the
          choice -- a refusal is only useful with the because. */}
      <fieldset className="rmproj-choice">
        <legend>The checkout at <code>{live}</code></legend>
        {checkout === null ? (
          <p className="rmproj-checking">Checking what is in it…</p>
        ) : (
          <>
            <label className={files === "keep" ? "is-chosen" : ""}>
              <input
                type="radio"
                name={`files-${name}`}
                checked={files === "keep"}
                onChange={() => setFiles("keep")}
              />
              <span>
                <strong>Leave it where it is</strong>
                <em>The directory, its branches and its remote stay exactly as they are.</em>
              </span>
            </label>
            <label className={files === "delete" ? "is-chosen" : ""}>
              <input
                type="radio"
                name={`files-${name}`}
                checked={files === "delete"}
                disabled={!checkout.removable}
                onChange={() => setFiles("delete")}
              />
              <span>
                <strong>Delete it from this machine</strong>
                <em>
                  {checkout.removable
                    ? `For a repository Persei cloned and you no longer want here. ${checkout.reason}.`
                    : `Not offered: ${checkout.reason}.`}
                </em>
              </span>
            </label>
          </>
        )}
      </fieldset>

      <label className="rmproj-type">
        <span>Type <code>{name}</code> to confirm</span>
        <input
          className="wiz-input"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          aria-label={`Type ${name} to confirm removal`}
        />
      </label>

      <div className="rmproj-actions">
        <button
          type="button"
          className="rmproj-go"
          disabled={typed !== name || busy}
          onClick={() => void go()}
        >
          {busy ? "Removing…"
            : files === "delete" ? "Remove and delete the checkout"
            : memory === "archive" ? "Remove and archive" : "Remove and delete"}
        </button>
        <button type="button" className="wiz-btn" disabled={busy} onClick={reset}>
          Cancel
        </button>
      </div>
      {error && <p className="wiz-error" role="alert">{error}</p>}
    </div>
  );
}
