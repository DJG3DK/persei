/* oxlint-disable react/only-export-components -- helpers exported for their tests; an edit here remounts the file under Fast Refresh, which is fine */
import { memo, useState } from "react";
import { modelColor, relativeTime, shortModel } from "../format";
import { artifactImages, splitArtifactImages } from "../artifactImages";
import type { LogEntry } from "../types";
import { PerseiMark } from "./PerseiMark";
import "./ChatMessage.css";

/**
 * Classifies a raw LogEntry into a chat-shaped rendering. The stream's
 * entries are heterogeneous (agent prose, tool calls, tool results, gate
 * verdicts, operator nudges) — a chat UI needs each to look like what it IS
 * rather than uniform boxed rows.
 */
type Kind = "agent" | "tool-call" | "tool-result" | "system" | "user";

function classify(entry: LogEntry): Kind {
  if (entry.node === "operator") return "user";
  if (entry.node === "verify_and_ship") return "system";
  // The supervisor's heals and conclusions (agent/supervisor.py) are the
  // system acting on the task, not the agent speaking.
  if (entry.node === "supervisor") return "system";
  // A heartbeat ("ping") or any entry without text must never take the page
  // down: the error boundary swallowed the whole planning view on 2026-09-09
  // when a ping was wrapped as a log entry and reached this with no summary.
  const s = entry.summary ?? "";
  if (s.startsWith("calling: ")) return "tool-call";
  if (s.startsWith("tool result:")) return "tool-result";
  if (s.startsWith("awaiting approval")) return "system";
  return "agent";
}

/**
 * Agent message content arrives as the Python repr of LangChain content
 * blocks ("[{'type': 'text', 'text': '...'}]"). Extract the human text
 * instead of showing the raw structure — the single biggest "ugly and
 * boxed" complaint about the old view.
 */
export function cleanText(raw: string): string {
  if (!raw) return "";
  if (!raw.trimStart().startsWith("[{")) return raw;
  const parts: string[] = [];
  const re = /'text':\s*(['"])((?:[^\\]|\\.)*?)\1(?:,|\})/gs;
  let m: RegExpExecArray | null;
  while ((m = re.exec(raw)) !== null) {
    parts.push(
      m[2]
        .replace(/\\n/g, "\n")
        .replace(/\\'/g, "'")
        .replace(/\\"/g, '"')
        .replace(/\\\\/g, "\\"),
    );
  }
  if (parts.length) return parts.join("\n\n");
  // Server-side truncation (detail is capped at 2000 chars) can cut the repr
  // mid-string so the quote never closes and the strict regex finds nothing.
  // Lenient fallback: strip the structural prefix/suffix and unescape.
  const prefix = raw.match(/^\s*\[\{'type':\s*'text',\s*'text':\s*(['"])/);
  if (prefix) {
    let body = raw.slice(prefix[0].length);
    body = body.replace(/(['"]),\s*'index':\s*\d+\}\]\s*$/, "").replace(/(['"])\}\]\s*$/, "");
    return body
      .replace(/\\n/g, "\n")
      .replace(/\\'/g, "'")
      .replace(/\\"/g, '"')
      .replace(/\\\\/g, "\\");
  }
  return raw;
}

/** Matches a hex color (#fff or #a1b2c3) or an rgb()/rgba() function call --
 * the concrete, parseable ways a color actually gets written in prose. Not
 * CSS keyword names ("teal", "cyan") -- those are ambiguous across
 * palettes/rendering contexts in a way a literal value isn't. */
const COLOR_PATTERN = /#(?:[0-9a-fA-F]{3}){1,2}\b|rgba?\([\d.,\s%]+\)/g;

/** Renders `text` as plain strings interleaved with a small swatch
 * immediately after every hex/rgb color value found in it -- planning
 * chat's system prompt (agent/planning_chat.py) is told to always include a
 * real value alongside any color it discusses, specifically so this can
 * show the color itself rather than making the user picture it from a code
 * or a name. Swatches are visual-only (aria-hidden); the color text itself
 * remains screen-reader visible. */
export function renderWithColorSwatches(text: string): React.ReactNode {
  const re = new RegExp(COLOR_PATTERN);
  const nodes: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) nodes.push(text.slice(lastIndex, match.index));
    const color = match[0];
    nodes.push(color);
    nodes.push(<span key={`swatch-${key++}`} className="color-swatch" style={{ background: color }} title={color} aria-hidden="true" />);
    lastIndex = match.index + color.length;
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex));
  return nodes.length > 1 ? nodes : text;
}

function ArtifactImage({ caption, url }: { caption: string; url: string }) {
  return (
    <figure className="chat-image">
      <a href={url} target="_blank" rel="noopener noreferrer" title="Open full size">
        <img src={url} alt={caption} loading="lazy" />
      </a>
      {caption && <figcaption>{caption}</figcaption>}
    </figure>
  );
}

/** Prose with the agent's shown images drawn in place, and colour swatches
 * in the text between them. */
export function RichText({ text }: { text: string }) {
  const parts = splitArtifactImages(text);
  if (parts.length === 1 && parts[0].kind === "text") return <>{renderWithColorSwatches(text)}</>;
  return (
    <>
      {parts.map((p, i) => p.kind === "image"
        ? <ArtifactImage key={i} caption={p.caption} url={p.url} />
        : p.text.trim() ? <span key={i}>{renderWithColorSwatches(p.text)}</span> : null)}
    </>
  );
}

/** The images in a tool result, shown without opening the output -- they are
 * the point of the call. */
export function ToolResultImages({ body }: { body: string }) {
  const images = artifactImages(body);
  if (!images.length) return null;
  return (
    <div className="chat-images">
      {images.map((im) => <ArtifactImage key={im.url} caption={im.caption} url={im.url} />)}
    </div>
  );
}

/** "calling: bash({'command': 'ls -la'})" -> [{name: "bash", args: "..."}] */
export function parseToolCalls(summary: string): { name: string; args: string }[] {
  const body = summary.slice("calling: ".length);
  const calls: { name: string; args: string }[] = [];
  const re = /(\w+)\((.*?)\)(?:, (?=\w+\())?/gs;
  let m: RegExpExecArray | null;
  while ((m = re.exec(body)) !== null) {
    calls.push({ name: m[1], args: m[2] });
  }
  return calls.length ? calls : [{ name: "tool", args: body }];
}

function agentName(node: string): string {
  if (node.startsWith("work:")) return node.slice("work:".length);
  return "agent";
}

function verdictTone(summary: string): "good" | "bad" | "warn" | "info" {
  const s = summary.toLowerCase();
  if (s.includes("ready") || s.includes("merged and deployed") || s.includes("done")) return "good";
  if (s.includes("escalated") || s.includes("failed")) return "bad";
  if (s.includes("needs_fixes") || s.includes("nudging") || s.includes("awaiting")) return "warn";
  return "info";
}

/** Role badge beside the model badge. Two roles can pin the SAME model
 *  (coder and test-writer both on deepseek), so the model badge alone cannot
 *  attribute an error in the stream to a role. Muted styling on purpose —
 *  the role is orientation, the model stays the colored primary badge. */
function RoleBadge({ role }: { role?: string | null }) {
  if (!role) return null;
  return (
    <span className="chat-role-badge" title={`agent role: ${role}`}>
      {role}
    </span>
  );
}

export function ModelBadge({ model }: { model?: string | null }) {
  if (!model) return null;
  const color = modelColor(model);
  return (
    <span className="chat-model-badge" style={{ color, borderColor: color + "55", background: color + "14" }} title={model}>
      {shortModel(model)}
    </span>
  );
}

/** Click-and-keyboard toggle for a collapsed log row.

A `<div onClick>` is what these used to be. That is a mouse-only control:
no role, no tab stop, no aria-expanded, and Space/Enter do nothing. The
rows that have nothing to expand stay a plain div, so a screen reader is
not offered a button that cannot open. */
function ExpandToggle({
  open,
  onToggle,
  className,
  label,
  children,
}: {
  open: boolean;
  onToggle: () => void;
  className: string;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={className}
      role="button"
      tabIndex={0}
      aria-expanded={open}
      aria-label={label}
      onClick={onToggle}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onToggle();
        }
      }}
    >
      {children}
    </div>
  );
}

export const TOOL_ICONS: Record<string, string> = {
  bash: "❯_",
  read: "📄",
  write: "✏️",
  edit: "✏️",
  read_file: "📄",
  write_file: "✏️",
  edit_file: "✏️",
  ls: "📁",
  glob: "📁",
  grep: "🔎",
  task: "🤝",
  run_checks: "🧪",
  write_todos: "☑️",
};

function ChatMessageImpl({ entry, prevEntry }: { entry: LogEntry; prevEntry?: LogEntry }) {
  const kind = classify(entry);
  const [open, setOpen] = useState(false);

  // Nothing to show (a heartbeat that slipped into the log, or a malformed
  // entry): render nothing rather than an empty bubble or a crash.
  if (!entry.summary && !entry.detail) return null;

  if (kind === "user") {
    return (
      <div className="chat-row chat-row--user">
        <div className="chat-bubble chat-bubble--user">
          <div className="chat-text">{renderWithColorSwatches(cleanText(entry.detail || entry.summary.replace(/^\[operator message\]\s*/, "")))}</div>
        </div>
        <span className="chat-time">{relativeTime(entry.timestamp)}</span>
      </div>
    );
  }

  if (kind === "system") {
    const tone = verdictTone(entry.summary);
    const detail = cleanText(entry.detail || "");
    const line = (
      <>
        <span className="chat-system-icon">
          {tone === "good" ? "✓" : tone === "bad" ? "✕" : tone === "warn" ? "!" : "•"}
        </span>
        <span className="chat-system-text">{entry.summary}</span>
        {detail && <span className={`chat-chevron ${open ? "open" : ""}`}>▸</span>}
      </>
    );
    return (
      <div className={`chat-system chat-system--${tone}`}>
        {detail ? (
          <ExpandToggle
            className="chat-system-line"
            open={open}
            onToggle={() => setOpen((o) => !o)}
            label={open ? "Hide gate detail" : "Show gate detail"}
          >
            {line}
          </ExpandToggle>
        ) : (
          <div className="chat-system-line">{line}</div>
        )}
        {open && detail && <div className="chat-system-detail">{detail}</div>}
      </div>
    );
  }

  if (kind === "tool-call") {
    const calls = parseToolCalls(entry.summary);
    return (
      <div className="chat-tools">
        <RoleBadge role={entry.role} /><ModelBadge model={entry.model} />
        <span className="chat-time">{relativeTime(entry.timestamp)}</span>
        {calls.map((c, i) => (
          <span key={i} className="chat-tool-chip" title={c.args}>
            <span className="chat-tool-icon">{TOOL_ICONS[c.name] ?? "⚙"}</span>
            <span className="chat-tool-name">{c.name}</span>
            <span className="chat-tool-args">{c.args.slice(0, 64)}</span>
          </span>
        ))}
      </div>
    );
  }

  if (kind === "tool-result") {
    const body = entry.detail || entry.summary.slice("tool result:".length).trim();
    // rg/grep exit 1 is "no matches", and the bash tool says so in the result
    // (agent/tools/agent_tools.py NO_MATCHES_RESULT). Everything else non-zero is a failure.
    const noMatches = /^exit_code=1 \(no matches/.test(body);
    const failed = !noMatches && /exit_code=[1-9]|ERROR|TIMED OUT/.test(body.slice(0, 120));
    return (
      <div className={`chat-tool-result ${failed ? "chat-tool-result--failed" : ""}`}>
        <ExpandToggle
          className="chat-tool-result-head"
          open={open}
          onToggle={() => setOpen((o) => !o)}
          label={open ? "Hide tool output" : "Show tool output"}
        >
          <span className={`chat-chevron ${open ? "open" : ""}`}>▸</span>
          <span>{failed ? "output — error" : "output"}</span>
          <span className="chat-time">{relativeTime(entry.timestamp)}</span>
          <code className="chat-tool-result-preview">{body.replace(/\s+/g, " ").slice(0, 90)}</code>
        </ExpandToggle>
        <ToolResultImages body={body} />
        {open && <pre className="chat-tool-result-body">{body}</pre>}
      </div>
    );
  }

  // agent prose
  const name = agentName(entry.node);
  // audit H-16: React.memo on this component (see export below) is what keeps
  // this cleanText + renderWithColorSwatches pass from re-running for every
  // entry on each parent re-render -- it now runs only when this entry's own
  // props change. (Can't useMemo here: this is past the early returns above,
  // and a conditional hook violates rules-of-hooks.)
  const text = cleanText(entry.detail || entry.summary);
  const sameSpeaker = prevEntry && classify(prevEntry) === "agent" && agentName(prevEntry.node) === name;
  return (
    <div className={`chat-row chat-row--agent ${sameSpeaker ? "chat-row--cont" : ""}`}>
      {/* One mark for every speaker, coordinator and subagent alike. The
          subagents used to wear a tinted disc with their initials, on the
          reasoning that two in a row need telling apart -- but the NAME is
          already on the line beside it ("test-writer", "investigator"), so
          the disc was a second and louder answer to a question the label had
          already answered, and it made one transcript look like two different
          products talking. */}
      {!sameSpeaker && (
        <div className="chat-avatar chat-avatar--mark">
          <PerseiMark size={26} />
        </div>
      )}
      <div className="chat-agent-col">
        {!sameSpeaker && (
          <div className="chat-agent-name">
            {name === "agent" ? "Agent" : name}
            <RoleBadge role={entry.role} /><ModelBadge model={entry.model} />
            <span className="chat-time">{relativeTime(entry.timestamp)}</span>
            {entry.cost_usd > 0 && <span className="chat-cost">${entry.cost_usd.toFixed(3)}</span>}
          </div>
        )}
        <div className="chat-bubble chat-bubble--agent">
          <div className="chat-text"><RichText text={text} /></div>
        </div>
      </div>
    </div>
  );
}

// audit H-16: the task log renders unvirtualized and grows without bound on a
// long autonomous run. Wrapping in React.memo means an appended entry only
// renders the NEW ChatMessage rather than re-running cleanText /
// renderWithColorSwatches across every entry ever received (was O(N) per event,
// O(N^2) cumulative). entry / prevEntry are referentially stable per index --
// useTaskStream appends with [...log, ...new], preserving prior references.
export const ChatMessage = memo(ChatMessageImpl);
