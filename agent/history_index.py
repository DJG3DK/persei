"""A keyword index over what past tasks ran into.

Every terminal task writes an episode: the goal, how it ended, what it cost
and -- when it went wrong -- the reason. There are ~458,000 tokens of them
and nothing can query a single one. The consolidation agent distils the
recurring patterns into memory, which is the right thing to do with a
pattern and the wrong thing to do with a one-off: a single three-month-old
escalation that says exactly why this merge cannot fast-forward is not a
pattern, so it never reaches memory, and it is unreachable from anywhere
else.

Worse than unreachable. agent/consolidation.py's _prune_consolidated_episodes
DELETEs episodes past the retention window, so that history is not merely
unqueryable, it is on a path to being destroyed. This table is therefore
also the archive: a row demoted to source_live = FALSE still carries the
text after the store row is gone.

Why a table of our own
----------------------
langgraph's `store` table is not ours to index, for three reasons and only
the first is about ownership:

* Pruning. An index keyed to store rows loses exactly the old one-off this
  exists to find, at exactly the moment it is deleted.
* Write amplification. A GIN expression index on `store` is maintained on
  EVERY store write, and agent/planning_log.py's Recorder.flush rewrites a
  jsonb document of up to 2000 entries every 20 seconds during a live turn.
  That is a cost on the hot path of the thing that must not slow down.
* The text we want to search is not the text the row stores. An episode is
  a FileData document whose `content` is a JSON string; a task log is
  {"entries": [...]}. Each needs its own extraction and its own field
  weighting, and an expression index over `value` can do neither.

So: a sibling table in the same database, migrated the way langgraph
migrates its own -- a version counter and a list of statements applied by
index, every one of them idempotent, safe to run on every startup.

Weighting is the whole design
-----------------------------
`err` (an episode's escalation_reason, a task log's error lines) is weight
A, the one-line label is B, everything else is D. Measured on the real 146
episodes: a weighted ts_rank_cd for "fast-forward merge failed" returned the
four merge failures, with the two carrying the error in escalation_reason
above the two that only mention merging in prose. Unweighted, that query is
a grep.

Reading it back
---------------
search() ranks, fetch() opens one record whole, and neither is reached by an
agent directly. Both go through agent/episode_recall.py: this module
registers itself there as a retrieval LEG when an index is installed, and
the tools in agent/tools/history_tools.py ask the registry rather than this
table. That indirection is the only thing that lets a second way of looking
things up -- an embedding, later -- arrive without touching a tool, a seat
or a prompt.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from agent import episode_recall
from agent.backends import backend_for_dsn
from agent.config import Config
from agent.episode_recall import EpisodeHit
from agent.project_removal import namespaces as _project_namespaces
from agent.store_paging import all_items

logger = logging.getLogger("tektonix")

TABLE = "agent_history_fts"
MIGRATIONS_TABLE = "agent_history_fts_migrations"

CORPUS_EPISODE = "episode"
CORPUS_TASK = "task"
CORPUS_TASK_LOG = "task_log"

# Which store namespace each corpus is extracted from, named by the label
# agent/project_removal.namespaces() uses rather than spelled out again --
# that function is the canonical list of what a project owns, and a second
# copy of ("episodes", repo) here is a copy that can drift from the one the
# removal path walks. A renamed label fails loudly with a KeyError instead.
_CORPUS_LABEL = {
    CORPUS_EPISODE: "episodes",
    CORPUS_TASK: "tasks",
    CORPUS_TASK_LOG: "task_log",
}

# planning_log is deliberately absent. It is 15 items and ~418,000 tokens --
# the worst chunk-cost-to-value ratio of the four corpora -- it is the
# planner's own transcript, and it is the one extractor with no explicit
# outcome field to put in weight A. It is one extractor to add later against
# a proven index, not a reason to make the first one bigger.
CORPORA: tuple[str, ...] = (CORPUS_EPISODE, CORPUS_TASK, CORPUS_TASK_LOG)


# Bodies are split at this many characters so one tsvector stays far under
# Postgres' 1 MB limit and ts_headline stays cheap. The real driver is the
# task log: one live row holds 2000 entries and 1.3 MB of text.
CHUNK_CHARS = 8000

# A chunk whose text is empty indexes to an empty tsvector and can never
# match anything, so it is dropped rather than stored.
_MIN_CHUNK_CHARS = 1


def namespaces_for(corpus: str, repo: str) -> tuple[str, ...]:
    """The store namespace a corpus is read from."""
    return _project_namespaces(repo)[_CORPUS_LABEL[corpus]]


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

# Applied by list index against a version counter, the way
# AsyncPostgresStore.setup() applies its own MIGRATIONS -- so a statement
# added below runs once, on the next start, on every installation, and this
# module never has to ask whether it is a fresh database or an old one.
#
# Nothing here touches `store`, `store_migrations`, `checkpoints`,
# `checkpoint_blobs`, `checkpoint_writes` or `checkpoint_migrations`.
# langgraph's setup() runs on the same database on every start and must stay
# correct; an additive table of our own is invisible to it.
MIGRATIONS: tuple[str, ...] = (
    f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    corpus      TEXT NOT NULL,
    repo        TEXT NOT NULL,
    item_key    TEXT NOT NULL,
    chunk_no    INTEGER NOT NULL DEFAULT 0,
    occurred_at TIMESTAMPTZ NOT NULL,
    task_id     TEXT,
    session_id  TEXT,
    outcome     TEXT,
    label       TEXT NOT NULL DEFAULT '',
    err         TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    source_live BOOLEAN NOT NULL DEFAULT TRUE,
    indexed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (corpus, repo, item_key, chunk_no)
)
""",
    # A GENERATED column rather than a trigger: to_tsvector(regconfig, text)
    # with an explicit configuration is IMMUTABLE, which is what Postgres
    # requires here, and a generated column cannot drift out of step with
    # the text the way a trigger someone forgets to fire can.
    f"""
ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS tsv tsvector
GENERATED ALWAYS AS (
    setweight(to_tsvector('english', err),   'A') ||
    setweight(to_tsvector('english', label), 'B') ||
    setweight(to_tsvector('english', body),  'D')
) STORED
""",
    # Not CONCURRENTLY: that cannot run inside a transaction block, so it
    # would constrain every caller's connection to autocommit forever to buy
    # nothing -- the table is empty when this first runs.
    f"CREATE INDEX IF NOT EXISTS {TABLE}_tsv_idx ON {TABLE} USING gin (tsv)",
    f"CREATE INDEX IF NOT EXISTS {TABLE}_repo_idx ON {TABLE} (repo, corpus, occurred_at DESC)",
    # The path column, and the three statements it takes to get it into the
    # tsvector. Postgres' parser turns a whole path into ONE lexeme, so a
    # reader who types the relative path, or just the filename, matched
    # nothing at all while the answer sat in the index: measured on the live
    # corpus, the lexeme 'config.yaml' was in 0 chunks although that file's
    # absolute path was in two. _path_terms writes every trailing run of the
    # path's segments here, so both spellings are lexemes of their own.
    f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS paths TEXT NOT NULL DEFAULT ''",
    # A generation expression cannot be altered in place, so the column is
    # dropped and re-added. Additive in effect: the rows keep their text and
    # the new tsvector regenerates from it. The GIN index goes with the
    # column, which is why it is created again below.
    f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS tsv",
    f"""
ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS tsv tsvector
GENERATED ALWAYS AS (
    setweight(to_tsvector('english', err),   'A') ||
    setweight(to_tsvector('english', label), 'B') ||
    setweight(to_tsvector('english', paths), 'C') ||
    setweight(to_tsvector('english', body),  'D')
) STORED
""",
    # 'english' for the paths too, not 'simple': the query side stems with
    # 'english', and a segment indexed unstemmed is a segment the query can
    # no longer reach.
    f"CREATE INDEX IF NOT EXISTS {TABLE}_tsv_idx ON {TABLE} USING gin (tsv)",
)

# Held for the whole of ensure_schema. CREATE TABLE IF NOT EXISTS is NOT
# safe against a concurrent identical CREATE -- it races in the catalog, not
# on the name -- and four concurrent first migrations against a scratch
# schema returned one success and three UniqueViolations on
# pg_type_typname_nsp_index. install_for catches that and installs nothing,
# so the process that lost the race spends its whole life with no history
# index. The server's lifespan and the consolidation cron starting together
# on a fresh box is exactly that race.
_MIGRATION_LOCK = 0x41484653  # "AHFS" -- this table's migrations, nothing else


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

# What looks like a file path, or a bare filename, in a piece of stored text.
# Two shapes rather than one: a path has a slash in it, and a filename has a
# lowercase extension. The extension is required to be two characters or
# more and lowercase so that "e.g" and "thing.The" are not filenames.
_PATH_SHAPED = re.compile(
    r"[A-Za-z0-9_.+~@-]*(?:/[A-Za-z0-9_.+~@-]+)+"
    r"|[A-Za-z0-9_+~@-]+\.[a-z][a-z0-9]{1,7}\b"
)

# How many trailing segments of one path are written out. Four covers every
# way a person refers to a file in this tree -- the name, the directory and
# the name, and the couple of levels above that -- and stops a deeply nested
# path from spending the whole budget on prefixes nobody types.
_PATH_SUFFIXES = 4

# The cap on one row's worth of path terms. A build transcript mentions
# hundreds of files; past a few thousand characters they are no longer
# identifying the record, they are the record.
PATH_CHARS = 4000


def _decompose(token: str) -> list[str]:
    """Every spelling of one path a reader might type.

    The whole absolute path is already a lexeme of the body, and it is the
    one spelling nobody uses: an editor shows a relative path and a
    traceback shows a bare filename. So each TRAILING run of segments is
    emitted as its own term, plus the basename without its extension.
    """
    token = token.strip("/.")
    segments = [s for s in token.split("/") if s]
    if not segments:
        return []
    terms = ["/".join(segments[-n:]) for n in range(1, min(len(segments), _PATH_SUFFIXES) + 1)]
    stem, dot, _ext = segments[-1].rpartition(".")
    if dot and stem:
        terms.append(stem)
    return terms


def _path_terms(*texts: str) -> str:
    """The path spellings of `texts`, deduplicated and capped.

    Indexed in weight C, between the one-line label and the prose: a query
    that names a file is naming the record it wants far more precisely than
    one that shares a word with its body, and far less precisely than one
    that quotes the failure.
    """
    seen: set[str] = set()
    out: list[str] = []
    size = 0
    for text in texts:
        for token in _PATH_SHAPED.findall(text or ""):
            for term in _decompose(token):
                if term in seen:
                    continue
                if size + len(term) + 1 > PATH_CHARS:
                    return " ".join(out)
                seen.add(term)
                out.append(term)
                size += len(term) + 1
    return " ".join(out)


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HistoryRow:
    """One indexable chunk of one stored record.

    The three text fields are the weighting, and the weighting is what makes
    "have we hit this error before" work rather than "have we ever mentioned
    merging": `err` is the failure text and rides in weight A, `label` is
    the single line a digest would print, `body` is everything else.
    """

    corpus: str
    repo: str
    item_key: str
    chunk_no: int
    occurred_at: datetime
    task_id: str | None = None
    session_id: str | None = None
    outcome: str | None = None
    label: str = ""
    err: str = ""
    body: str = ""
    # FALSE once the record this came from has left the store. Carried on
    # the row rather than forced TRUE by the writer, so that restoring an
    # archive puts back what was archived: a demoted row whose store record
    # the pruner deleted would otherwise come back claiming the record is
    # still there.
    source_live: bool = True
    # Derived, never passed: the searchable spellings of whatever paths the
    # three text fields mention. init=False because there is one right
    # answer for a given text and a caller that could pass a different one
    # is a caller that can put the index out of step with the record --
    # including agent/project_removal.py's restore, which rebuilds rows from
    # an archive that predates this column.
    paths: str = field(default="", init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", _path_terms(self.err, self.label, self.body))


@dataclass
class SyncResult:
    """What one pass actually did, per corpus and in total.

    `written` counts rows whose text changed, not rows considered: the
    upsert below only touches a row whose content differs, so a second
    identical backfill reports zero and that is the idempotence claim being
    made rather than asserted.
    """

    items: int = 0
    rows: int = 0
    written: int = 0
    demoted: int = 0
    dropped: int = 0
    failed: list[str] = field(default_factory=list)
    # Why nothing was copied, when nothing was. '' means a sync actually
    # ran. This field exists because its absence was a hole with no floor:
    # a Postgres box whose install_for failed produced a SyncResult that was
    # byte-identical to a SQLite box's -- empty, failed == [] -- and
    # agent/consolidation.py then permanently DELETEd store rows on the
    # strength of it. 'no-index' is an installation that has none by design;
    # 'unavailable' is one that should have had one and could not open it.
    skipped: str = ""

    def merge(self, other: SyncResult) -> SyncResult:
        self.items += other.items
        self.rows += other.rows
        self.written += other.written
        self.demoted += other.demoted
        self.dropped += other.dropped
        self.failed.extend(other.failed)
        self.skipped = self.skipped or other.skipped
        return self

    @property
    def copied(self) -> bool:
        """Whether the archive copy this result describes actually happened.

        The only thing a caller about to DELETE the source rows may ask.
        Deliberately NOT `not self.failed`: an empty result with no failures
        is the answer both from an installation with no index and from one
        whose index could not be opened, and only the first of those is safe
        to prune behind.
        """
        return not self.failed and self.skipped != "unavailable"

    @property
    def why_not_copied(self) -> str:
        """One line naming what stopped the copy, for an operator's log."""
        if self.failed:
            return f"the index sync failed for {', '.join(sorted(set(self.failed)))}"
        if self.skipped == "unavailable":
            return "this installation should have a history index and could not open one"
        return ""


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

# C0 control characters other than tab and newline. A raw NUL reaching
# psycopg is not a bad search result, it is a ValueError on the insert --
# and one can arrive legitimately, because json.loads turns a backslash-u-0000 escape
# in stored content into a real NUL byte.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# What makes a task-log LINE worth weight A. Deliberately a shape, not a
# whitelist of messages: the question this index answers is "have we hit
# this before", and the lines that answer it are the ones that were already
# shouting.
_ERROR_SHAPE = re.compile(
    r"\b(error|errors|failed|failing|failure|traceback|exception|refused|rejected|"
    r"denied|timed out|timeout|not found|cannot|could not|unable to|fatal|abort\w*|"
    r"exit code [1-9])\b",
    re.IGNORECASE,
)

# How much of one chunk may be weight A. A transcript is full of the word
# "error", so matching whole ENTRIES put 20% of the whole task-log corpus in
# weight A when this was measured on the live data -- and a weight that
# nearly every row carries is not a weight, it is a constant, which would
# have buried the 21 real escalations under 400 build transcripts. Matching
# lines and capping them brings it to 6.5%.
ERR_CHARS = 2000


def _error_lines(text: str) -> str:
    """The lines of `text` that look like something going wrong."""
    return "\n".join(line for line in text.splitlines() if _ERROR_SHAPE.search(line))

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _clean(value: object) -> str:
    """Text safe to hand to Postgres, and nothing else changed.

    Not a sanitiser: the stored text is the point, and trimming it would
    lose the exact error string somebody will search for character by
    character.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
    return _CONTROL.sub(" ", value)


def _first_line(text: str, limit: int = 200) -> str:
    """The one line a digest would print. Markdown headings lose their
    hashes because a label reading "# Fix the thing" is a label that renders
    as a heading in whatever prints it next."""
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line:
            return line[:limit]
    return ""


def _when(*candidates: object) -> datetime:
    """The first candidate that parses as a time, else the epoch.

    The epoch rather than now(): a record with no usable timestamp is old,
    not new, and dating it now() would float it to the top of every
    recency-ordered read forever.
    """
    for candidate in candidates:
        if isinstance(candidate, datetime):
            return candidate if candidate.tzinfo else candidate.replace(tzinfo=UTC)
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            parsed = datetime.fromisoformat(candidate.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return _EPOCH


def _group(pieces: list[str], size: int = CHUNK_CHARS) -> list[list[int]]:
    """Which pieces belong to which chunk, as indices into `pieces`.

    Split on the boundaries the corpus already has -- one log entry, one
    line -- rather than at a character offset, so a chunk never ends
    mid-token and no search can miss a match that fell across the seam of an
    arbitrary cut. A single piece longer than `size` is kept whole for the
    same reason.

    Indices rather than text because the task-log extractor needs the SAME
    grouping twice, once for the body and once for the error lines within
    it, and deriving the boundaries a second time is how the two halves come
    to disagree about which chunk a failure was in.
    """
    groups: list[list[int]] = []
    current: list[int] = []
    length = 0
    for n, piece in enumerate(pieces):
        if not piece:
            continue
        if current and length + len(piece) > size:
            groups.append(current)
            current, length = [], 0
        current.append(n)
        length += len(piece) + (1 if length else 0)
    if current:
        groups.append(current)
    return [g for g in groups
            if len("\n".join(pieces[i] for i in g).strip()) >= _MIN_CHUNK_CHARS]


def _pack(pieces: list[str], size: int = CHUNK_CHARS) -> list[str]:
    """`_group`, joined back into the text of each chunk."""
    return ["\n".join(pieces[i] for i in g) for g in _group(pieces, size)]


def episode_rows(repo: str, item_key: str, record: dict,
                 fallback_when: object = None) -> list[HistoryRow]:
    """The corpus that earns the whole subsystem.

    escalation_reason is the only field in this system that holds the actual
    text of a failure -- "hint: Diverging branches can't be fast-forwarded",
    "Model call limits exceeded: run limit (400/400)" -- so it is the one
    that goes in weight A. Everything else about an episode is context for a
    hit, not the reason for one.
    """
    goal = _clean(record.get("goal"))
    outcome = _clean(record.get("outcome")) or None
    err = _clean(record.get("escalation_reason"))
    verdict = _clean(record.get("review_verdict"))
    label = " ".join(p for p in (outcome, _first_line(goal)) if p)
    detail = [
        goal,
        f"review verdict: {verdict}" if verdict else "",
        f"cost ${record.get('cost_usd')} over {record.get('iteration_count')} iteration(s)",
    ]
    when = _when(record.get("timestamp"), fallback_when)
    return [
        HistoryRow(
            corpus=CORPUS_EPISODE, repo=repo, item_key=item_key, chunk_no=n,
            occurred_at=when, task_id=_clean(record.get("task_id")) or None,
            outcome=outcome, label=label,
            # The error rides on every chunk. An episode is one record; a
            # hit on its third chunk still has to be able to say what went
            # wrong, and re-reading the first chunk to find out is a second
            # query for something already known.
            err=err, body=body,
        )
        for n, body in enumerate(_pack([d for d in detail if d]))
    ]


def task_rows(repo: str, item_key: str, value: dict,
              fallback_when: object = None) -> list[HistoryRow]:
    """Thin, cheap, and the spine that links the other two: a task row
    carries the task_id an episode and a task log are both keyed by."""
    goal = _clean(value.get("goal"))
    status = _clean(value.get("status")) or None
    err = _clean(value.get("escalation_reason"))
    detail = [
        goal,
        f"route: {_clean(value.get('route'))} -- {_clean(value.get('route_reason'))}"
        if value.get("route") or value.get("route_reason") else "",
        f"todos: {_clean(value.get('latest_todos'))}" if value.get("latest_todos") else "",
        f"category: {_clean(value.get('category'))}" if value.get("category") else "",
    ]
    when = _when(value.get("created_at"), fallback_when)
    return [
        HistoryRow(
            corpus=CORPUS_TASK, repo=repo, item_key=item_key, chunk_no=n,
            occurred_at=when, task_id=_clean(value.get("task_id")) or item_key,
            outcome=status, label=" ".join(p for p in (status, _first_line(goal)) if p),
            err=err, body=body,
        )
        for n, body in enumerate(_pack([d for d in detail if d]))
    ]


def task_log_rows(repo: str, item_key: str, value: dict,
                  fallback_when: object = None, goal: str = "") -> list[HistoryRow]:
    """The richest corpus for HOW something was fixed, and the one nothing
    in this system reads today.

    A task log is a transcript, so its `err` cannot be one field: the error
    text is scattered across whichever entries happened to go wrong. Those
    entries' text is collected into weight A for the chunk they fall in --
    per chunk, not per item, because a 2000-entry log covering half a day of
    work has no single failure to speak of.

    `goal` is the goal of the task this is the transcript OF, joined on the
    key by the caller. Build logs are more than half the index and the top
    hit for most queries, so their label is the line a reader most often has
    to choose from -- and without the goal it was the transcript's first
    entry, which is whatever tool the task happened to call first: "tool
    result: exit_code=0", "calling: write_todos({...". None of those names
    what the task was for. The first entry stays as the fallback for a
    transcript whose task row is gone.
    """
    entries = value.get("entries")
    if not isinstance(entries, list):
        return []
    pieces: list[str] = []
    errors: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        summary = _clean(entry.get("summary"))
        detail = _clean(entry.get("detail"))
        text = summary if summary == detail else "\n".join(p for p in (summary, detail) if p)
        if not text:
            continue
        pieces.append(text)
        errors.append(_error_lines(text))

    first = entries[0] if entries and isinstance(entries[0], dict) else {}
    when = _when(first.get("timestamp"), value.get("updated_at"), fallback_when)
    label = _first_line(_clean(goal)) or _first_line(_clean(first.get("summary")))
    return [
        HistoryRow(
            corpus=CORPUS_TASK_LOG, repo=repo, item_key=item_key, chunk_no=n,
            occurred_at=when, task_id=_clean(first.get("step_id")) or item_key,
            session_id=_clean(value.get("session_id")) or None,
            label=label,
            err="\n".join(errors[i] for i in group if errors[i])[:ERR_CHARS],
            body="\n".join(pieces[i] for i in group),
        )
        for n, group in enumerate(_group(pieces))
    ]


_EXTRACTORS = {
    CORPUS_EPISODE: episode_rows,
    CORPUS_TASK: task_rows,
    CORPUS_TASK_LOG: task_log_rows,
}


def rows_for_item(corpus: str, repo: str, item, goal: str = "") -> list[HistoryRow] | None:
    """Extract one store item, or None when the extraction FAILED.

    The distinction is not cosmetic and it is not for the logs. An empty
    list means "this record legitimately has nothing to index"; None means
    "this record has something and it could not be read". sync_corpus hands
    a count of rows to drop_tail_chunks, and a count of zero deletes every
    chunk the item has -- so an extractor that starts raising on a class of
    records would, on the next nightly pass and silently, delete exactly the
    archive this subsystem was argued for. Never raises either way: one
    unreadable record must cost its own row and not the rest of the pass.
    """
    value = getattr(item, "value", None)
    if not isinstance(value, dict):
        return []
    when = getattr(item, "created_at", None) or getattr(item, "updated_at", None)
    try:
        if corpus == CORPUS_EPISODE:
            record = _episode_record(value)
            # `is not None`, not truthiness: an episode whose stored content
            # parses to an empty object is a record with nothing in it,
            # which is not the same as content that would not parse at all
            # -- and _episode_record already draws that line by returning
            # None. Conflating them made the second look like the first.
            return episode_rows(repo, item.key, record, when) if record is not None else None
        if corpus == CORPUS_TASK_LOG:
            return task_log_rows(repo, item.key, value, when, goal)
        return _EXTRACTORS[corpus](repo, item.key, value, when)
    except Exception as e:  # noqa: BLE001 -- see the docstring
        logger.warning("history index: could not extract %s %s: %s", corpus, item.key, e)
        return None


def _episode_record(value: dict) -> dict | None:
    """An episode's real fields, out of the FileData document it is stored
    in. agent/episodes.py writes it through StoreBackend, which keeps the
    record as a JSON string in `content`; nothing about the episode itself
    is visible at the top level of the stored value."""
    content = value.get("content")
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return None
    try:
        record = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return record if isinstance(record, dict) else None


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

# How many hits a caller gets when it does not say, and the most it can ask
# for. The cap is the point: a digest of twenty hits is already most of a
# page of context spent on things the model has not decided to read.
DEFAULT_LIMIT = 8
MAX_LIMIT = 20

# ts_rank_cd's weights, in Postgres' own {D, C, B, A} order. These ARE the
# defaults, written out because they are the whole ranking: A is the failure
# text and it is worth ten of the surrounding prose. Searching for "merge
# failed" must not rank a task that merely mentions merging above the one
# whose escalation_reason says the merge failed.
RANK_WEIGHTS = [0.1, 0.2, 0.4, 1.0]

# Two short fragments, not a paragraph. A digest exists so a model can pick
# which record to open; a snippet long enough to answer the question would
# make the digest cost what reading the records costs.
_HEADLINE_OPTS = ("MaxFragments=2,MaxWords=14,MinWords=5,"
                  "StartSel=<,StopSel=>,FragmentDelimiter= ... ")
# ts_headline re-parses the text it is given, so handing it a 1.3 MB build
# transcript is a real cost per hit for fragments that come from the first
# few kilobytes anyway.
_HEADLINE_CHARS = 12_000

# Below this many hits the precise stage is treated as having missed, and
# the widened one runs as well. Two hits is not "the answer"; it is often
# one accidental word match.
_STAGE_1_ENOUGH = 3

# A lexeme in more than this fraction of the corpus carries no information
# about which record is wanted -- in a corpus of build episodes that is
# "task", "file", "test", "error". Dropping them is what stops the widened
# stage returning most of the table. Counted in RECORDS; see _DF_SQL.
_DF_MAX_FRACTION = 0.30

# How many of the query's own lexemes the widened stage must still have
# after the document-frequency cut before it is allowed to run at all.
#
# Without this, a question the system has never seen came back as a
# confident full page: nothing required more than ONE lexeme to survive, so
# "redis cluster failover latency" widened to 'latenc' alone and returned
# five hits about an unrelated health-endpoints task, and "how do I rotate
# the VAPID key" widened to 'key' alone and returned eight about a
# strategy-removal feature. A query that collapses to a single ordinary
# word is a miss, and the miss message already tells the model what to do
# next -- which is far better than an answer that looks useful and is not,
# because nobody reports that one.
_WIDE_MIN_LEXEMES = 2

# The most query text that reaches a tsquery.
#
# websearch_to_tsquery does not deduplicate terms, so a long paste becomes a
# tsquery of thousands of nodes and ts_rank_cd is evaluated against every
# matching row. Measured on the live index: 1,200 characters took 0.19s,
# 6,000 took 5.66s and 24,000 took 23.67s, with numnode() at 7,999 -- and
# in the server these run on agent/auth.py's pool, the same five connections
# that answer logins. The query text is model-authored, so nothing upstream
# bounds it. 2,000 characters is several times the longest useful paste.
MAX_QUERY_CHARS = 2000

# The deadline on one search statement, the way agent/tools/project_db.py
# puts one on a model-authored SELECT. The cap above is the first line and
# this is the floor under it: a query shape nobody predicted must cost one
# slow response, never a pool with no free connection left for a login.
SEARCH_TIMEOUT_MS = 5000

# How many records one stage fetches per hit the caller asked for. The
# chunk dedup happens in SQL now, so the only thing left that can shorten a
# page is the task-level fold in search() -- three episodes and a task row
# describing one piece of work are one entry, not four.
_FOLD_FETCH = 3


class SearchTimeout(Exception):
    """A search statement hit SEARCH_TIMEOUT_MS.

    Distinct from every other failure in this module on purpose. `_ranked`
    treats a query the parser rejects as a miss and returns nothing, which
    is right -- but a timeout returning nothing is a search that quietly
    finds no history when there is history, which is the one symptom of
    this subsystem nobody would ever report.
    """


def _is_timeout(e: Exception) -> bool:
    """Whether `e` is a statement we cancelled ourselves.

    By SQLSTATE rather than by exception type so that this module still
    never imports psycopg on the server's path -- see _dsn_connect.
    """
    return getattr(e, "sqlstate", None) == "57014"  # query_canceled

# The widened stage ORs its terms, so its tail is everything that shares one
# word with the query. Cut it at a quarter of the best score: what survives
# is what matched several terms or matched them in weight A.
_TAIL_CUTOFF = 0.25

# The precise and widened stages, named. These are SQL function names, never
# anything a caller supplies -- the text being searched is always bound as a
# parameter, on both sides of the ladder.
_TSQ_PRECISE = "websearch_to_tsquery('english', %s)"
_TSQ_WIDE = "to_tsquery('english', %s)"

# One record per hit, decided in SQL by a window function rather than by
# over-fetching chunks and collapsing them in Python.
#
# The over-fetch was a real, measured bug and not a tidiness question: it
# took limit * 4 CHUNKS, and on the live index one build transcript held 17
# of the 32 chunks fetched for "failed", so a page of 8 came back with 5
# records out of 95 that matched -- and the tool printed "5 match(es)" as
# though that were the answer. Raising the multiplier does not fix it; at
# 320 chunks one record took 70. The docstring's objection to DISTINCT ON
# -- that it must order by the key before the score and so hands back the
# wrong chunk's snippet -- is true of DISTINCT ON and not of row_number,
# which orders WITHIN the partition.
#
# The page is cut before ts_headline runs. An expensive function in the
# target list of an ORDER BY ... LIMIT is evaluated for every sorted row,
# so the headline has to be projected onto an already-limited relation.
_SEARCH_SQL = """
WITH q AS (SELECT {tsq} AS tsq),
     hit AS (
       SELECT corpus, repo, item_key, chunk_no, occurred_at, task_id, outcome, label,
              source_live, err, body,
              ts_rank_cd(%s::float4[], tsv, q.tsq) AS score
       FROM {table}, q
       WHERE tsv @@ q.tsq
         AND repo = ANY(%s::text[]) AND corpus = ANY(%s::text[])
         AND (%s::timestamptz IS NULL OR occurred_at >= %s::timestamptz)
     ),
     best AS (
       SELECT hit.*, row_number() OVER (PARTITION BY corpus, repo, item_key
                                        ORDER BY score DESC, chunk_no) AS rn
       FROM hit
     ),
     page AS (
       SELECT * FROM best WHERE rn = 1 ORDER BY score DESC, occurred_at DESC LIMIT %s
     )
SELECT page.corpus, page.repo, page.item_key, page.chunk_no, page.occurred_at,
       page.task_id, page.outcome, page.label, page.source_live, page.score,
       ts_headline('english',
                   left(concat_ws(chr(10), nullif(page.err, ''), page.body), %s),
                   q.tsq, %s) AS snippet
FROM page, q
ORDER BY page.score DESC, page.occurred_at DESC
"""

# One round trip for the whole stop-list decision: the query's own lexemes,
# how many RECORDS in scope each one is in, and how many records there are.
# Doing it per lexeme would be a query per word of a pasted traceback.
#
# Records on both sides of the ratio, not chunks. Chunking is wildly uneven
# -- a handful of build transcripts hold more than half the rows -- so a
# term that lives in a few long transcripts counted as a term that is
# everywhere: measured on the live index, '/workspace' was in 50.8% of
# chunks and 5.2% of records, 'rg' 36.2% against 4.7%, 'git' 42.4% against
# 15.5%. All three were over the cut and were dropped, so the widened stage
# was discarding precisely the distinctive tokens -- paths, commands, tool
# names -- that identify a build transcript, which is the one thing it
# exists to rescue.
_DF_SQL = """
WITH lex AS (SELECT DISTINCT lexeme FROM unnest(to_tsvector('english', %s))),
     scope AS (SELECT count(DISTINCT (corpus, repo, item_key))::float AS total FROM {table}
                WHERE repo = ANY(%s::text[]) AND corpus = ANY(%s::text[]))
SELECT lex.lexeme AS lexeme,
       scope.total AS total,
       (SELECT count(DISTINCT (f.corpus, f.repo, f.item_key)) FROM {table} f
         WHERE f.repo = ANY(%s::text[]) AND f.corpus = ANY(%s::text[])
           AND f.tsv @@ plainto_tsquery('english', lex.lexeme)) AS ndoc
FROM lex, scope
"""

_FETCH_SQL = """
SELECT corpus, repo, item_key, chunk_no, occurred_at, task_id, session_id,
       outcome, label, err, body, source_live
FROM {table}
WHERE corpus = %s AND repo = %s AND item_key = %s
ORDER BY chunk_no
"""


@dataclass(frozen=True)
class Hit:
    """One record a search found, at the granularity a reader cares about.

    An item, not a chunk: a four-chunk build transcript that matches in
    three of them is one thing to go and read, and letting it take three
    slots of a page would push out three other records.
    """

    corpus: str
    repo: str
    item_key: str
    occurred_at: datetime
    label: str = ""
    snippet: str = ""
    outcome: str | None = None
    task_id: str | None = None
    source_live: bool = True
    score: float = 0.0
    # Which stage of the ladder produced it. Carried because "the precise
    # query found nothing and this is the widened one" is the difference
    # between a hit worth trusting and a hit worth glancing at -- and it is
    # PRINTED, because a difference carried into an extra dict and never
    # shown to the reader is a difference nobody acts on.
    stage: str = ""
    # The other corpora describing this same piece of work, folded in here
    # rather than taking slots of their own. An episode, its near-duplicate
    # and the task row are three records and one thing to go and read: on
    # the live index a page of eight for one query resolved to three
    # distinct task ids, so five of the eight slots said nothing new.
    also: tuple[str, ...] = ()

    @property
    def ref(self) -> str:
        """What a reader quotes back to open this record.

        corpus:repo:item_key, in that order, because the item key is the
        only part that can contain a colon -- an episode key carries an
        ISO timestamp -- so splitting from the left is unambiguous.
        """
        return f"{self.corpus}:{self.repo}:{self.item_key}"


def parse_ref(ref: str) -> tuple[str, str, str]:
    """corpus, repo, item_key out of a ref. Raises ValueError on anything
    that is not one, because a malformed ref reaching SQL as three empty
    strings looks exactly like a record that does not exist."""
    parts = (ref or "").split(":", 2)
    if len(parts) != 3 or not all(p.strip() for p in parts):
        raise ValueError(f"{ref!r} is not a history ref -- they look like corpus:project:key")
    return parts[0], parts[1], parts[2]


def _fold_by_task(hits: list[Hit]) -> list[Hit]:
    """One entry per piece of WORK, not per record.

    An episode, the near-duplicate episode written seventy seconds later and
    the task row are three items describing one task, so the record-level
    dedup in SQL does not see them -- and on the live index a page of eight
    resolved to three distinct task ids, spending five slots on things the
    reader had already been told. The task id is on every row and was going
    unused; the argument the design made for chunk dedup ("a four-chunk log
    does not consume the whole result page") simply applies one level up.

    Order is preserved, so the entry kept is the best-ranked of its group
    and the corpora folded into it are named on `also` rather than dropped.
    A hit with no task id folds with nothing: it is its own piece of work.
    """
    out: list[Hit] = []
    where: dict[tuple[str, str], int] = {}
    for hit in hits:
        if not hit.task_id:
            out.append(hit)
            continue
        key = (hit.repo, hit.task_id)
        if key not in where:
            where[key] = len(out)
            out.append(hit)
            continue
        kept = out[where[key]]
        if hit.corpus != kept.corpus and hit.corpus not in kept.also:
            out[where[key]] = replace(kept, also=kept.also + (hit.corpus,))
    return out


def _lexeme_literal(lexeme: str) -> str:
    """One lexeme as a quoted tsquery term.

    Quoted because a lexeme off a real corpus is not an identifier: it can
    be `src/core/backtester.js`, a hex sha or a version string, and
    unquoted those are tsquery syntax errors rather than search terms.
    """
    return "'" + lexeme.replace("'", "''") + "'"

# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------

class PostgresHistoryIndex:
    """The index on the database this installation already runs on.

    Takes a callable returning a connection context manager rather than a
    pool or a DSN, because the two callers want different things and neither
    should open a second pool against the same database: the server hands it
    agent/auth.py's existing pool, and a cron script hands it a one-shot
    connection.
    """

    def __init__(self, connect):
        self._connect = connect
        self._schema_ready = False

    async def ensure_schema(self) -> int:
        """Apply any unapplied migration. Safe on every startup, and safe
        against a second process doing it at the same moment BECAUSE of the
        advisory lock, not because the statements say IF NOT EXISTS.

        That distinction was a real hole rather than a pedantry. CREATE
        TABLE IF NOT EXISTS races in the catalog, not on the name: four
        concurrent first migrations against a scratch schema returned one
        success and three UniqueViolations on pg_type_typname_nsp_index.
        install_for catches an exception here by installing NO index, so the
        process that lost the race -- the server's lifespan, or the nightly
        cron, whichever started second on a fresh box -- would have run with
        no history search and no episode indexing until it was restarted.

        Returns the number of statements applied, which is 0 on every start
        after the first.
        """
        applied = 0
        async with self._connect() as conn:
            # Session-level, and released in the finally below, because this
            # connection is usually the server's pooled one and would carry
            # the lock back into the pool. The same shape langgraph's own
            # setup() uses for its migrations on this database.
            #
            # The release is reliable because BOTH connection paths are
            # autocommit -- agent/auth.py's pool sets it and _dsn_connect
            # passes it -- so a failed statement above does not leave a
            # transaction in the aborted state that would reject the unlock
            # and strand the lock on a pooled connection.
            await conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
            try:
                await conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (v INTEGER PRIMARY KEY)")
                cur = await conn.execute(
                    f"SELECT v FROM {MIGRATIONS_TABLE} ORDER BY v DESC LIMIT 1")
                row = await cur.fetchone()
                version = _version_of(row)
                for v, sql in enumerate(MIGRATIONS[version + 1:], start=version + 1):
                    await conn.execute(sql)
                    await conn.execute(
                        f"INSERT INTO {MIGRATIONS_TABLE} (v) VALUES (%s) ON CONFLICT DO NOTHING",
                        (v,))
                    applied += 1
            finally:
                try:
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))
                except Exception as e:  # noqa: BLE001 -- never replace the error on its way out
                    logger.error("history index: the migration lock was not released: %s", e)
        self._schema_ready = True
        if applied:
            logger.info("history index: applied %d migration(s)", applied)
        return applied

    async def ensure_schema_once(self) -> None:
        """The migration, at most once per index object. The statements are
        idempotent, so this is about not making a round trip per episode."""
        if not self._schema_ready:
            await self.ensure_schema()

    async def upsert(self, rows: list[HistoryRow]) -> int:
        """Write rows, returning how many actually CHANGED.

        The conflict clause updates only a row whose text differs, so
        running the same extraction twice is a genuine no-op rather than a
        no-op that rewrites every row and churns the GIN index. That is also
        what makes "run the backfill twice and compare" a real assertion.
        """
        if not rows:
            return 0
        sql = f"""
            INSERT INTO {TABLE}
                (corpus, repo, item_key, chunk_no, occurred_at, task_id, session_id,
                 outcome, label, err, body, paths, source_live, indexed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (corpus, repo, item_key, chunk_no) DO UPDATE SET
                occurred_at = EXCLUDED.occurred_at,
                task_id     = EXCLUDED.task_id,
                session_id  = EXCLUDED.session_id,
                outcome     = EXCLUDED.outcome,
                label       = EXCLUDED.label,
                err         = EXCLUDED.err,
                body        = EXCLUDED.body,
                paths       = EXCLUDED.paths,
                source_live = EXCLUDED.source_live,
                indexed_at  = now()
            WHERE {TABLE}.occurred_at IS DISTINCT FROM EXCLUDED.occurred_at
               OR {TABLE}.task_id     IS DISTINCT FROM EXCLUDED.task_id
               OR {TABLE}.session_id  IS DISTINCT FROM EXCLUDED.session_id
               OR {TABLE}.outcome     IS DISTINCT FROM EXCLUDED.outcome
               OR {TABLE}.label       IS DISTINCT FROM EXCLUDED.label
               OR {TABLE}.err         IS DISTINCT FROM EXCLUDED.err
               OR {TABLE}.body        IS DISTINCT FROM EXCLUDED.body
               OR {TABLE}.paths       IS DISTINCT FROM EXCLUDED.paths
               OR {TABLE}.source_live IS DISTINCT FROM EXCLUDED.source_live
        """
        params = [
            (r.corpus, r.repo, r.item_key, r.chunk_no, r.occurred_at, r.task_id,
             r.session_id, r.outcome, r.label, r.err, r.body, r.paths, r.source_live)
            for r in rows
        ]
        async with self._connect() as conn, conn.cursor() as cur:
            await cur.executemany(sql, params)
            return max(cur.rowcount, 0)

    async def drop_tail_chunks(self, corpus: str, repo: str, counts: dict[str, int]) -> int:
        """Remove chunks past the end of each item's current extraction.

        An extractor that changes, or a record that shrank, leaves the old
        tail behind -- rows that still match searches for text the record no
        longer contains, which is worse than a missing hit because it is
        confidently wrong.
        """
        if not counts:
            return 0
        keys = list(counts)
        async with self._connect() as conn, conn.cursor() as cur:
            await cur.execute(
                f"""
                DELETE FROM {TABLE} f
                USING (SELECT * FROM unnest(%s::text[], %s::int[]) AS t(item_key, n)) t
                WHERE f.corpus = %s AND f.repo = %s
                  AND f.item_key = t.item_key AND f.chunk_no >= t.n
                """,
                (keys, [counts[k] for k in keys], corpus, repo),
            )
            return max(cur.rowcount, 0)

    async def demote_missing(self, corpus: str, repo: str, live_keys: list[str]) -> int:
        """Mark rows whose store record is gone as index-only.

        A demotion, never a delete: _prune_consolidated_episodes removing
        the store row is exactly the case this table exists to survive. The
        flag is what lets a later reader say "store row pruned -- index copy
        only" instead of silently serving a record that no longer exists.

        Only ever called with the full live key set for the namespace, from
        a read that succeeded. A partial list here would demote history that
        is still there.
        """
        async with self._connect() as conn, conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE {TABLE} SET source_live = FALSE, indexed_at = now()
                WHERE corpus = %s AND repo = %s AND source_live
                  AND NOT (item_key = ANY(%s::text[]))
                """,
                (corpus, repo, live_keys),
            )
            return max(cur.rowcount, 0)

    async def dump_project(self, repo: str) -> list[dict]:
        """Every row this project owns, as plain dicts for an archive.

        `tsv` is deliberately not read back: it is derived from the three
        text columns and regenerates itself on restore, so carrying it would
        put a stale copy of the same data in the archive file.
        """
        async with self._connect() as conn, conn.cursor() as cur:
            await cur.execute(
                f"""SELECT corpus, repo, item_key, chunk_no, occurred_at, task_id, session_id,
                           outcome, label, err, body, source_live
                    FROM {TABLE} WHERE repo = %s ORDER BY corpus, item_key, chunk_no""",
                (repo,),
            )
            return await _as_dicts(cur)

    async def restore_project(self, repo: str, rows: list[dict]) -> int:
        """Put an archive's rows back under `repo`.

        Under the CURRENT project name, not the archived one, for the same
        reason agent/project_removal.restore works that way: a hand-edited
        archive must not be able to write into a namespace nobody asked for.
        """
        if not rows:
            return 0
        return await self.upsert([
            HistoryRow(
                corpus=str(r.get("corpus") or ""), repo=repo,
                item_key=str(r.get("item_key") or ""), chunk_no=int(r.get("chunk_no") or 0),
                occurred_at=_when(r.get("occurred_at")), task_id=r.get("task_id"),
                session_id=r.get("session_id"), outcome=r.get("outcome"),
                label=_clean(r.get("label")), err=_clean(r.get("err")),
                body=_clean(r.get("body")), source_live=bool(r.get("source_live", True)),
            )
            for r in rows if r.get("corpus") and r.get("item_key")
        ])

    async def forget_project(self, repo: str) -> int:
        """Remove every row this project owns.

        The FTS table is not a store namespace, so nothing in
        project_removal.namespaces() reaches it -- without this call a
        removed project's searchable history stays behind forever, which is
        precisely the class of leftover that test exists to catch.
        """
        async with self._connect() as conn, conn.cursor() as cur:
            await cur.execute(f"DELETE FROM {TABLE} WHERE repo = %s", (repo,))
            return max(cur.rowcount, 0)

    async def counts(self) -> dict[str, int]:
        """Rows per corpus, for an operator asking whether the backfill ran."""
        async with self._connect() as conn, conn.cursor() as cur:
            await cur.execute(
                f"SELECT corpus, count(*) AS n FROM {TABLE} GROUP BY corpus ORDER BY 1")
            return {row["corpus"]: row["n"] for row in await _as_dicts(cur)}


    async def search(self, query: str, *, repos: list[str],
                     corpora: tuple[str, ...] = CORPORA,
                     since: datetime | None = None,
                     limit: int = DEFAULT_LIMIT) -> list[Hit]:
        """Rank the corpus against a question, best first.

        A ladder of two stages, because one query shape cannot serve both
        things this gets asked. `websearch_to_tsquery` ANDs every term,
        which is right for "fast-forward merge failed" and wrong for a
        pasted traceback: one token that has since moved -- a line number,
        a sha, a renamed path -- takes recall to zero. Measured on the real
        corpus, the AND of a realistic paste returned 4 rows and an
        unbounded OR of the same words returned 86 of 146, so neither alone
        is a search.

        So: the precise stage first, and if it found barely anything, the
        widened one as well -- its terms filtered by document frequency and
        its tail cut at a fraction of the top score. The precise hits keep
        the top of the list, because a record that matched every term is a
        better answer than one that matched three of eight, and the widened
        stage is there to fill a page that would otherwise be empty.

        There is no third, fuzzy stage. It was cut deliberately: it fires
        only when both of these return nothing, it needs an extension
        installed on a live database, and it buys what an embedding leg
        buys better -- see agent/episode_recall.py.

        Raises SearchTimeout, and nothing else: see _ranked.
        """
        repos = [r for r in repos if r]
        query = query.strip()[:MAX_QUERY_CHARS]
        if not query or not repos:
            return []
        limit = max(1, min(limit, MAX_LIMIT))
        fetch = limit * _FOLD_FETCH
        hits = _fold_by_task(
            await self._ranked(_TSQ_PRECISE, query, repos, corpora, since, fetch, "precise"))
        if len(hits) >= _STAGE_1_ENOUGH:
            return hits[:limit]
        wide = await self._widened_query(query, repos, corpora)
        if not wide:
            return hits[:limit]
        widened = await self._ranked(_TSQ_WIDE, wide, repos, corpora, since, fetch, "widened")
        if widened:
            floor = widened[0].score * _TAIL_CUTOFF
            widened = [h for h in widened if h.score >= floor]
        seen = {h.ref for h in hits}
        return _fold_by_task(hits + [h for h in widened if h.ref not in seen])[:limit]

    @asynccontextmanager
    async def _reading(self):
        """A connection with a deadline on it.

        SET rather than SET LOCAL because these connections are autocommit
        -- SET LOCAL outside a transaction sets nothing and warns -- and
        reset in the finally because the server's is a POOLED connection
        that would otherwise carry the deadline back to whatever borrows it
        next. agent/tools/project_db.py puts the same ceiling on the other
        statement in this system whose text a model wrote.
        """
        async with self._connect() as conn:
            await conn.execute(f"SET statement_timeout = {SEARCH_TIMEOUT_MS}")
            try:
                yield conn
            finally:
                try:
                    await conn.execute("SET statement_timeout = DEFAULT")
                except Exception as e:  # noqa: BLE001 -- see below
                    # The reset must never replace the error already on its
                    # way out -- a SearchTimeout reported as "could not
                    # reset the deadline" is the wrong thing entirely. A
                    # connection this broken is one the pool's own check
                    # discards before anyone borrows it again.
                    logger.warning("history index: could not reset the deadline: %s", e)

    async def _ranked(self, tsq: str, text: str, repos: list[str], corpora: tuple[str, ...],
                      since: datetime | None, limit: int, stage: str) -> list[Hit]:
        """One stage of the ladder, one Hit per record, already ranked.

        The dedup is the window function in _SEARCH_SQL, so what comes back
        is `limit` RECORDS rather than limit chunks of however few records
        happened to be long.
        """
        sql = _SEARCH_SQL.format(tsq=tsq, table=TABLE)
        params = (text, RANK_WEIGHTS, repos, list(corpora), since, since, limit,
                  _HEADLINE_CHARS, _HEADLINE_OPTS)
        async with self._reading() as conn, conn.cursor() as cur:
            try:
                await cur.execute(sql, params)
            except Exception as e:  # noqa: BLE001 -- classified immediately below
                if _is_timeout(e):
                    # Not a miss. Returning [] here would be a search that
                    # silently found no history in a corpus that has some.
                    logger.error("history index: %s stage timed out after %dms on a "
                                 "%d-character query", stage, SEARCH_TIMEOUT_MS, len(text))
                    raise SearchTimeout(
                        f"the history search took longer than {SEARCH_TIMEOUT_MS}ms") from e
                logger.warning("history index: %s stage failed: %s", stage, e)
                return []
            rows = await _as_dicts(cur)
        return [
            Hit(
                corpus=row["corpus"], repo=row["repo"], item_key=row["item_key"],
                occurred_at=row["occurred_at"], label=row["label"] or "",
                snippet=row["snippet"] or "", outcome=row["outcome"],
                task_id=row["task_id"], source_live=bool(row["source_live"]),
                score=float(row["score"] or 0.0), stage=stage,
            )
            for row in rows
        ]

    async def _widened_query(self, query: str, repos: list[str],
                             corpora: tuple[str, ...]) -> str:
        """The query's terms, minus the ones that say nothing, ORed.

        The document-frequency filter is the whole reason an OR is usable
        here. Without it the common words of this corpus -- the ones every
        build episode contains -- match most of the table and the ranking
        has nothing to work with.

        When every term is common, they are all kept rather than none: a
        query made entirely of ordinary words is still a question, and the
        score cutoff the caller applies is what keeps the answer short.

        What is NOT allowed is a multi-word question collapsing to one
        ordinary word -- see _WIDE_MIN_LEXEMES.
        """
        async with self._reading() as conn, conn.cursor() as cur:
            try:
                await cur.execute(_DF_SQL.format(table=TABLE),
                                  (query, repos, list(corpora), repos, list(corpora)))
                rows = await _as_dicts(cur)
            except Exception as e:  # noqa: BLE001 -- see _ranked
                if _is_timeout(e):
                    logger.error("history index: widening timed out after %dms on a "
                                 "%d-character query", SEARCH_TIMEOUT_MS, len(query))
                    raise SearchTimeout(
                        f"the history search took longer than {SEARCH_TIMEOUT_MS}ms") from e
                logger.warning("history index: could not widen the query: %s", e)
                return ""
        present = [r for r in rows if (r["ndoc"] or 0) > 0]
        total = float(rows[0]["total"]) if rows else 0.0
        keep = [r["lexeme"] for r in present if not total or r["ndoc"] <= total * _DF_MAX_FRACTION]
        if not keep:
            keep = [r["lexeme"] for r in present]
        if len(rows) >= _WIDE_MIN_LEXEMES and len(keep) < _WIDE_MIN_LEXEMES:
            return ""
        # The path spellings of whatever the query named, alongside the
        # whole token. Postgres makes ONE lexeme of a path, so a reader who
        # typed the relative form was asking for a lexeme the corpus does
        # not contain even though the file is all over it; the index writes
        # the same suffixes into weight C -- see _path_terms -- and this is
        # the other half of that join.
        terms = list(dict.fromkeys(keep + [t for lex in keep for t in _decompose(lex)]))
        return " | ".join(_lexeme_literal(lex) for lex in terms)

    async def fetch(self, corpus: str, repo: str, item_key: str) -> dict[str, Any] | None:
        """One whole indexed record, its chunks reassembled in order.

        Served from this table rather than from the store on purpose. The
        index IS the archive -- that is the property the subsystem was
        argued for -- so a record whose store row the pruner deleted reads
        exactly the same way as one still there, through one code path that
        cannot develop a second set of bugs for the case nobody exercises.
        """
        async with self._connect() as conn, conn.cursor() as cur:
            await cur.execute(_FETCH_SQL.format(table=TABLE), (corpus, repo, item_key))
            rows = await _as_dicts(cur)
        if not rows:
            return None
        errs: list[str] = []
        for row in rows:
            err = (row["err"] or "").strip()
            if err and err not in errs:
                errs.append(err)
        return {
            "corpus": corpus, "repo": repo, "item_key": item_key,
            "occurred_at": rows[0]["occurred_at"], "task_id": rows[0]["task_id"],
            "session_id": rows[0]["session_id"], "outcome": rows[0]["outcome"],
            "label": rows[0]["label"] or "",
            # One shared failure reason is an episode's escalation_reason and
            # it appears nowhere else in the text. Several different ones are
            # a transcript's error LINES, which were lifted out of the body
            # below and would be printed twice.
            "err": errs[0] if len(errs) == 1 else "",
            "text": "\n".join(row["body"] or "" for row in rows),
            "chunks": len(rows),
            "source_live": bool(rows[0]["source_live"]),
        }


async def _as_dicts(cur) -> list[dict]:
    """Rows as dicts whatever the connection's row factory is.

    The server hands this module agent/auth.py's pool, which is dict_row; a
    cron script hands it a connection of its own, which is not. Naming the
    columns off cur.description rather than assuming one of the two is what
    stops a query working in the server and returning tuples in the script.
    """
    columns = [c.name for c in cur.description or []]
    return [row if isinstance(row, dict) else dict(zip(columns, row, strict=True))
            for row in await cur.fetchall()]


def _version_of(row) -> int:
    """The applied migration version, from whichever row shape the
    connection's row factory produced -- the server's auth pool is
    dict_row, a script's own connection is not."""
    if row is None:
        return -1
    if isinstance(row, dict):
        return int(row["v"])
    return int(row[0])


# ---------------------------------------------------------------------------
# opening, and the process default
# ---------------------------------------------------------------------------

def open_index(config: Config, *, pool=None) -> PostgresHistoryIndex | None:
    """The index for this installation, or None when there is not one.

    None is a supported state, not an error: a caller treats it as "no
    history index", the same way agent/tools/logo_tools.py's installed()
    lets a seat be built without a tool that could only ever fail.

    Opens no connection. The first statement does that, so importing this
    module and building an index cost nothing.
    """
    backend = backend_for_dsn(config.dsn)
    if backend != "postgres":
        # The SQLite half is FTS5 and it lands with the SQLite backend
        # itself. Building it now would mean writing an index for a store
        # agent/graph.py still refuses to open, and testing it against
        # dependencies this installation deliberately does not install.
        logger.debug("history index: no implementation for the %s backend yet", backend)
        return None
    if pool is not None:
        return PostgresHistoryIndex(lambda: pool.connection())
    return PostgresHistoryIndex(_dsn_connect(config.pg_dsn))


def _dsn_connect(dsn: str):
    @asynccontextmanager
    async def connect():
        import psycopg  # noqa: PLC0415 -- a script-only path; the server passes a pool

        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
        try:
            yield conn
        finally:
            await conn.close()

    return connect


# ---------------------------------------------------------------------------
# the retrieval leg
# ---------------------------------------------------------------------------

LEG_NAME = "fts"


async def fts_leg(store, repo: str, query: str, *, limit: int = DEFAULT_LIMIT,
                  repos: list[str] | None = None, corpora: tuple[str, ...] = CORPORA,
                  since: datetime | None = None, index: PostgresHistoryIndex | None = None,
                  **kwargs) -> list[EpisodeHit]:
    """This index, as one way of answering "have we hit this before".

    Registered with agent/episode_recall.py rather than called directly, so
    that the day an embedding leg lands, nothing about the tool, the seats
    or the prompts changes -- the fusion just has two rankings to combine
    instead of one.

    `store` is ignored here and taken anyway: it is part of the leg
    contract, because a leg that reads the store is the normal case and
    this one happening not to is an implementation detail of this one leg.

    `**kwargs` is not laziness either. Legs are called with one set of
    arguments by one caller, and a leg that raised TypeError on an argument
    meant for a different leg would take the whole search down with it --
    recall_episodes drops a leg that raises.
    """
    index = index or default_index()
    if index is None:
        return []
    hits = await index.search(query, repos=repos or [repo], corpora=corpora,
                              since=since, limit=limit)
    return [
        EpisodeHit(
            ref=hit.ref, repo=hit.repo, rank=n, snippet=hit.snippet, leg=LEG_NAME,
            extra={
                "corpus": hit.corpus, "item_key": hit.item_key,
                "occurred_at": hit.occurred_at, "label": hit.label,
                "outcome": hit.outcome, "task_id": hit.task_id,
                "source_live": hit.source_live, "stage": hit.stage,
                "also": hit.also,
            },
        )
        for n, hit in enumerate(hits, 1)
    ]


# The index this process writes through. Set once, explicitly, by whatever
# owns the process: the server in its lifespan, a cron script in its main().
#
# Explicit rather than opened on demand from the DSN, because the alternative
# is a module that reaches for the database the first time anything writes an
# episode -- including under the test suite, whose whole promise is that it
# connects to nothing. A caller that forgets to install one indexes nothing
# and says so in its summary; a caller that opens one by accident is a
# connection nobody asked for, in a process nobody expected to need one.
_DEFAULT: PostgresHistoryIndex | None = None


def install(index: PostgresHistoryIndex | None) -> None:
    global _DEFAULT
    _DEFAULT = index
    # The leg registers with the index and leaves with it, so
    # episode_recall.available() answers the operator's real question --
    # "can anything search my history in this process" -- rather than "was
    # this module imported". A leg that is registered and can only ever
    # return nothing is the shape of capability hole agent/capabilities.py
    # exists to close.
    if index is None:
        episode_recall.unregister_leg(LEG_NAME)
    else:
        episode_recall.register_leg(LEG_NAME, fts_leg)


def default_index() -> PostgresHistoryIndex | None:
    return _DEFAULT


async def install_for(config: Config, *, pool=None) -> PostgresHistoryIndex | None:
    """Open the index, migrate it, make it this process's default.

    Never raises. An index that cannot be reached must cost history search,
    never the startup of the server or the nightly job that called this.
    """
    try:
        index = open_index(config, pool=pool)
        if index is None:
            return None
        await index.ensure_schema()
    except Exception as e:  # noqa: BLE001 -- see the docstring
        logger.error("history index unavailable, history will not be searchable: %s", e)
        install(None)
        return None
    install(index)
    return index


def available(config: Config | None = None) -> bool:
    """Whether this installation has a history index at all.

    The capability convention (see agent/capabilities.py): an operator whose
    search comes back empty needs to know whether the corpus is empty, the
    index was never built, or the feature is not installed here. This
    answers the third and the second; the row counts answer the first.

    Synchronous and connection-opening on purpose -- its only caller is
    scripts/doctor.py, which runs when the installation is already suspect
    and must not depend on the server being up.
    """
    from agent.config import load_config  # noqa: PLC0415 -- doctor runs on a broken box

    config = config or load_config()
    if backend_for_dsn(config.dsn) != "postgres":
        return False
    import psycopg  # noqa: PLC0415

    with psycopg.connect(config.pg_dsn, connect_timeout=5) as conn:
        row = conn.execute("SELECT to_regclass(%s)", (TABLE,)).fetchone()
    return bool(row and row[0])


# ---------------------------------------------------------------------------
# the write paths
# ---------------------------------------------------------------------------

async def index_episode(config: Config, repo: str, item_key: str, record: dict) -> int:
    """Index one episode as it is written.

    At write time rather than at the next nightly sync because a fresh error
    is most valuable to the very next task, and one INSERT per terminal task
    outcome is not a cost worth deferring.

    Never raises: agent/episodes.py must ship the task whether or not the
    episode became searchable.
    """
    index = default_index()
    if index is None:
        if backend_for_dsn(config.dsn) == "postgres":
            # An installation that COULD index and is not: the process never
            # called install_for. Said once per episode at debug, because
            # the alternative is the question this whole capability
            # convention exists to answer -- "search returns nothing and
            # there is no way to tell whether the corpus is empty, the index
            # was never built, or nobody wired it up".
            logger.debug(
                "history index: %s is not indexing episodes -- nothing called install_for", repo)
        return 0
    try:
        await index.ensure_schema_once()
        return await index.upsert(episode_rows(repo, item_key, record))
    except Exception as e:  # noqa: BLE001 -- see the docstring
        logger.warning("history index: episode %s not indexed: %s", item_key, e)
        return 0


async def _task_goals(store, repo: str) -> dict[str, str]:
    """What each task was FOR, keyed the way a build transcript is keyed.

    A task row and its transcript are both stored under the task id, so the
    join is on the key and costs one read of a small namespace. Best-effort:
    a transcript whose goal cannot be found is labelled by its first entry,
    which is what every build log was labelled by before this.
    """
    try:
        items = await all_items(store, namespaces_for(CORPUS_TASK, repo))
    except Exception as e:  # noqa: BLE001 -- a label is not worth failing a sync over
        logger.warning("history index: could not read task goals for %s: %s", repo, e)
        return {}
    return {
        item.key: str((item.value or {}).get("goal") or "")
        for item in items if isinstance(getattr(item, "value", None), dict)
    }


async def index_task(config: Config, repo: str, task_id: str,
                     task_value: dict | None, log_value: dict | None) -> int:
    """Index a task and its build transcript NOW, because they are about to
    be deleted.

    The two corpora that have no write-time hook. An episode is indexed as
    agent/episodes.py writes it; a task row and its transcript are indexed
    only by the nightly sync_project, so a task deleted from the dashboard
    before the next nightly run was never indexed at ALL -- and demote_missing
    cannot rescue it, because there is no row to demote. That is the same
    ordering agent/consolidation.py is careful about, in a second place and
    with nothing enforcing it.

    Never raises, and the caller deletes whether or not this worked: a
    stored record that is not searchable is a gap the next sync closes, and
    that is a different thing from a deleted record that was never indexed.
    """
    index = default_index()
    if index is None:
        if backend_for_dsn(config.dsn) == "postgres":
            logger.warning(
                "history index: %s/%s is being deleted and was never indexed -- "
                "nothing called install_for in this process", repo, task_id)
        return 0
    rows: list[HistoryRow] = []
    try:
        if isinstance(task_value, dict):
            rows.extend(task_rows(repo, task_id, task_value))
        if isinstance(log_value, dict):
            goal = _clean(task_value.get("goal")) if isinstance(task_value, dict) else ""
            rows.extend(task_log_rows(repo, task_id, log_value, None, goal))
        if not rows:
            return 0
        await index.ensure_schema_once()
        return await index.upsert(rows)
    except Exception as e:  # noqa: BLE001 -- see the docstring
        logger.warning("history index: task %s not indexed before deletion: %s", task_id, e)
        return 0


async def sync_corpus(index: PostgresHistoryIndex, repo: str, store, corpus: str, *,
                      goals: dict[str, str] | None = None) -> SyncResult:
    """Reconcile one corpus for one project against the store."""
    result = SyncResult()
    namespace = namespaces_for(corpus, repo)
    try:
        items = await all_items(store, namespace)
    except Exception as e:  # noqa: BLE001 -- a namespace that cannot be read is not a namespace that is empty
        logger.warning("history index: could not read %s for %s: %s", corpus, repo, e)
        result.failed.append(corpus)
        return result

    rows: list[HistoryRow] = []
    counts: dict[str, int] = {}
    live: list[str] = []
    for item in items:
        extracted = rows_for_item(corpus, repo, item, (goals or {}).get(item.key, ""))
        # Three outcomes, and only one of them may reach `counts`.
        #
        # counts is what drop_tail_chunks deletes past, so counts[key] = 0
        # is "delete every chunk this item has" -- a HARD delete of the
        # archived copy, at the very moment agent/consolidation.py is about
        # to delete the store row as well. An extraction that failed (None)
        # or came back empty therefore leaves the existing chunks alone and
        # costs at worst a stale row, which is the mistake with an undo.
        #
        # `live` is separate from `counts` for the same reason: the item IS
        # still in the store whether or not it extracted, and demoting it to
        # index-only would be a lie about where the record is.
        live.append(item.key)
        if extracted is None:
            result.failed.append(f"{corpus}:{item.key}")
            continue
        if not extracted:
            continue
        rows.extend(extracted)
        counts[item.key] = len(extracted)
    result.items = len(items)
    result.rows = len(rows)
    result.written = await index.upsert(rows)
    result.dropped = await index.drop_tail_chunks(corpus, repo, counts)
    # Only after a read that succeeded -- see demote_missing.
    result.demoted = await index.demote_missing(corpus, repo, live)
    return result


async def sync_project(config: Config, repo: str, store, *,
                       index: PostgresHistoryIndex | None = None,
                       corpora: tuple[str, ...] = CORPORA) -> SyncResult:
    """Bring the whole index for one project in step with the store.

    THE ORDERING THIS EXISTS FOR: agent/consolidation.py calls this BEFORE
    _prune_consolidated_episodes, which permanently DELETEs store rows. An
    episode deleted before it is indexed is gone, silently and for good.
    See the call site.

    Never raises. The nightly job's real work is memory, and a search index
    that cannot be written must not take that down with it.
    """
    result = SyncResult()
    index = index or default_index()
    if index is None:
        # Which KIND of nothing this is. On Postgres a missing default index
        # is an installation that should have had one and did not -- nobody
        # called install_for, or it failed -- and the caller is about to
        # delete the rows this was supposed to copy. See SyncResult.copied.
        result.skipped = ("unavailable" if backend_for_dsn(config.dsn) == "postgres"
                          else "no-index")
        if result.skipped == "unavailable":
            logger.error(
                "history index: %s was not indexed -- this installation is on Postgres and "
                "nothing installed an index in this process", repo)
        return result
    try:
        await index.ensure_schema_once()
        goals = await _task_goals(store, repo) if CORPUS_TASK_LOG in corpora else {}
        for corpus in corpora:
            result.merge(await sync_corpus(index, repo, store, corpus, goals=goals))
    except Exception as e:  # noqa: BLE001 -- see the docstring
        logger.warning("history index: sync failed for %s: %s", repo, e)
        result.failed.append(repo)
    return result
