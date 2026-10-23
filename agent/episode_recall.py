"""Finding the episode that already answers this.

"Have we hit this before?" is one question, so it gets one answer, however
many ways there turn out to be of looking it up. A full-text index over the
episode text is one way. An embedding of the same text is another, and it
finds the case full text cannot: a months-old episode about a different part
of the tree that shares no words with today's task but describes the same
shape of problem.

Those are LEGS, not features. Each one registers here, each one returns its
own ranking, and the rankings are fused. Two retrieval tools over one corpus
would be a routing decision a model gets wrong half the time, and would cost
its description in prompt tokens in every seat that has it.

Fusion is reciprocal rank -- position, never score. A cosine distance and a
text rank are not on the same scale and nothing sensible calibrates them
against each other; their ORDERINGS are directly comparable, and that is all
RRF uses. It also gives this module the property the whole build order
depends on: a leg that is not registered contributes nothing, so with one
leg the fused ranking IS that leg's ranking, unchanged. Adding the second
leg is additive, and removing it again is a revert rather than a migration.

Two legs today: agent/history_index.py's full text and
agent/episode_vectors.py's embeddings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("tektonix")


@dataclass(frozen=True)
class EpisodeHit:
    """One episode a leg thinks is relevant.

    `ref` is the store key the episode was written under (see
    agent/episodes.py), and it is what a caller quotes back to read the whole
    thing. `rank` is this leg's own ordering, 1-based -- fusion uses it and
    nothing else, which is why no leg has to explain its score to any other.
    """

    ref: str
    repo: str
    rank: int
    snippet: str = ""
    leg: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# A leg is asked a question and returns its own ranked answer. It is given
# the store because the legs that exist in a local installation have nowhere
# else to read from.
Leg = Callable[..., Awaitable[list[EpisodeHit]]]

_LEGS: dict[str, Leg] = {}

# RRF's smoothing constant, 60 in the paper that introduced it and in most
# implementations since. It is what stops the top hit of one leg from
# outweighing every other leg combined.
_RRF_K = 60


def register_leg(name: str, leg: Leg) -> None:
    """Add a retrieval leg. Re-registering a name replaces it, so a module
    that is imported twice does not vote twice."""
    _LEGS[name] = leg


def unregister_leg(name: str) -> None:
    _LEGS.pop(name, None)


def registered_legs() -> tuple[str, ...]:
    return tuple(sorted(_LEGS))


def available() -> bool:
    """Whether recall can answer anything at all.

    The convention agent/tools/logo_tools.py established: an optional
    subsystem says whether it works, the factory that builds its tools
    returns nothing when it does not, and scripts/doctor.py prints the
    answer -- so an operator whose search comes back empty is told why
    rather than left to read three modules and guess.
    """
    return bool(_LEGS)


def fuse(rankings: list[list[EpisodeHit]], limit: int | None = None) -> list[EpisodeHit]:
    """Reciprocal-rank fusion of several legs' rankings into one.

    The rule, in full, because a reviewer has to be able to check it:

        score(d) = sum over legs of 1 / (60 + rank of d in that leg)

    where `d` is a piece of WORK, not a row -- see _group_key. An episode
    found by two legs beats one found by either alone; an episode found by
    one leg keeps that leg's ordering relative to its own results. Ties go
    to the better single-leg rank, then to the ref, so the order never
    depends on dict iteration.

    The property the whole build order rests on: with ONE ranking in, the
    order out is that ranking's order, unchanged. 1/(60+rank) is strictly
    decreasing in rank, no group can score twice from one leg, and the tie
    break is that same rank -- so removing the vector leg leaves full
    text's ordering exactly as it was. tests/test_episode_vectors.py
    asserts it rather than leaving it to be believed.

    Position, never score. A cosine similarity and a ts_rank_cd are not on
    the same scale and nothing sensible calibrates them; their ORDERINGS
    are directly comparable, and that is all this uses.
    """
    scores: dict[str, float] = {}
    best: dict[str, EpisodeHit] = {}
    legs: dict[str, set[str]] = {}
    rolled_up = _rolled_up_tasks(rankings)
    for ranking in rankings:
        # A leg that returns two hits for one task means two things worth
        # showing by that leg's own reckoning, and folding them here would
        # silently shorten its page -- which is how "with one leg the fused
        # order is that leg's order" stops being true.
        taken: set[str] = set()
        for hit in ranking:
            key = _group_key(hit, rolled_up)
            if key in taken:
                # Only reachable now for two ROLLUP hits at one task inside
                # one leg. Made unique per leg and rank rather than falling
                # back to the ref, which was a key another leg could arrive
                # at independently: that is how a fused page came to show
                # one episode at rank 1 and again at rank 3.
                key = f"{hit.leg}#{hit.rank}:{hit.ref}"
            taken.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + hit.rank)
            legs.setdefault(key, set()).add(hit.leg)
            # Keep the hit that ranked highest anywhere: its snippet is the
            # one most likely to show the reader why this came back.
            if key not in best or hit.rank < best[key].rank:
                best[key] = hit
    order = sorted(scores, key=lambda key: (-scores[key], best[key].rank, best[key].ref))
    fused = [
        EpisodeHit(
            ref=best[key].ref,
            repo=best[key].repo,
            rank=n,
            snippet=best[key].snippet,
            leg=best[key].leg,
            # Which legs actually found it, on the hit rather than in a
            # side table: it is what the digest uses to say "semantic
            # match" about a hit sharing no word with the query, and what
            # the telemetry writes down to answer, in a month, whether the
            # second leg earned its place.
            extra={**best[key].extra, "found_by": sorted(legs[key])},
        )
        for n, key in enumerate(order, 1)
    ]
    return fused[:limit] if limit else fused


# The corpora whose ref names ONE record. Anything else -- a `task:` or a
# `task_log:` ref -- is a rollup: full text folds an episode, its
# near-duplicate and the task row into whichever chunk ranked best, and the
# ref it returns stands for the whole task rather than for a document.
_RECORD_CORPORA = ("episode",)


def _corpus(hit: EpisodeHit) -> str:
    """Which corpus a hit's ref names. The legs put it in `extra`; the
    prefix of the ref (corpus:repo:item_key) is the fallback, because a
    grouping rule that silently treats an unlabelled hit as a rollup would
    fold two real episodes into one slot and never say so."""
    corpus = (hit.extra or {}).get("corpus")
    if corpus:
        return str(corpus)
    return hit.ref.split(":", 1)[0] if ":" in hit.ref else ""


def _rolled_up_tasks(rankings: list[list[EpisodeHit]]) -> set[str]:
    """The tasks some leg answered with a rollup rather than with a record.

    Computed over every ranking before any of them is scored, so the
    grouping does not depend on which leg happened to be asked first.
    """
    return {
        f"task={hit.repo}/{(hit.extra or {}).get('task_id')}"
        for ranking in rankings
        for hit in ranking
        if (hit.extra or {}).get("task_id") and _corpus(hit) not in _RECORD_CORPORA
    }


def _group_key(hit: EpisodeHit, rolled_up: AbstractSet[str] = frozenset()) -> str:
    """What counts as the same thing found twice.

    A hit that names a RECORD is that record, and its ref is the key. A hit
    that names a TASK stands for all of them, so it takes a task key -- and
    record hits for that same task join it, because otherwise one task would
    take two slots of a page saying the same thing.

    The earlier rule keyed EVERY hit with a task id on the task, and that
    was wrong on this corpus in the normal case: one task writes several
    episodes, so two genuinely different episodes collapsed into one slot
    whenever either leg found both -- and, interacting with the per-leg
    guard in fuse, produced a page showing one episode twice and dropping
    the other. Two episodes of one task are two records; only a rollup ref
    claims to be the task itself.
    """
    task_id = (hit.extra or {}).get("task_id")
    if not task_id:
        return hit.ref
    key = f"task={hit.repo}/{task_id}"
    if _corpus(hit) not in _RECORD_CORPORA:
        return key
    return key if key in rolled_up else hit.ref


async def recall_episodes(store: object, repo: str, query: str, *, limit: int = 20,
                          errors: list[str] | None = None, **kwargs: Any) -> list[EpisodeHit]:
    """Ask every registered leg, fuse what comes back.

    A leg that raises is dropped rather than allowed to fail the search: a
    broken index must degrade retrieval, never break the task that was
    merely curious.

    `errors`, when a caller passes a list, collects what those dropped legs
    said. Without it every failure arrived at the caller as an empty result,
    which is indistinguishable from a corpus that genuinely holds nothing --
    and "the search timed out" printed as "no history matched" is the one
    symptom of this subsystem nobody would ever report.
    """
    # Concurrently, because the legs cost different things and neither
    # waits on the other: full text is one database query, and the vector
    # leg spends a network round trip embedding the query before it can ask
    # anything. Run in sequence the search would cost the sum, on a tool a
    # seat is meant to be able to reach for on a hunch. Order is fixed so
    # the fused result does not depend on which leg answered first.
    names = sorted(_LEGS)
    answers = await asyncio.gather(
        *(_LEGS[name](store, repo, query, limit=limit, **kwargs) for name in names),
        return_exceptions=True,
    )
    rankings: list[list[EpisodeHit]] = []
    for name, answer in zip(names, answers, strict=True):
        if isinstance(answer, BaseException) and not isinstance(answer, Exception):
            # CancelledError (and KeyboardInterrupt, and SystemExit) is not
            # a leg failing, it is this task being torn down. gather's
            # return_exceptions hands it back like any other result, and
            # logging it as a warning would swallow a cancellation and let
            # the search return a page to a caller that no longer exists.
            raise answer
        if isinstance(answer, Exception):
            logger.warning("episode recall: leg %s failed: %s", name, answer)
            if errors is not None:
                errors.append(f"{name}: {answer}")
            continue
        rankings.append(answer)
    return fuse(rankings, limit=limit)


# --- telemetry -------------------------------------------------------------
#
# Whether the second leg is worth building is a question about this corpus,
# not about retrieval in general -- it is small and it grows slowly, and full
# text may simply be enough. The only way to answer it is to record what was
# asked and whether the answer got used, starting now, so that by the time
# the decision is due there is a number rather than an argument.
#
# The measure is deliberately narrow: of the searches that ran, how many
# returned nothing the task went on to read. Two lines per search, appended,
# trimmed by size, and never allowed to raise -- the same contract as
# agent/tool_events.py, for the same reason.

LOG_PATH = Path(
    os.environ.get("AGENT_RETRIEVAL_LOG")
    or (Path(__file__).resolve().parents[1] / "logs" / "retrieval_events.jsonl")
)
MAX_BYTES = 5_000_000
_KEEP_FRACTION = 0.5

# How many of the returned refs are written down. The question being asked
# is "was there a hit in the top few", so the tail costs bytes and answers
# nothing.
TOP_N = 5


# What the searches, and the memory index, put in front of each task, so
# a use can be recorded WITH ITS RANK. The rank is the number the
# re-ranker decision hangs on (docs/todo.md, hybrid retrieval): a used
# episode at rank six or worse says ordering is the problem; one that was
# never offered says the candidate pool is. Without it the log held the
# answer's two halves in separate events and nobody joined them
# (2026-09-29). Every offered ref is kept, not the written head, so rank
# seven is recorded as seven and not as "never offered"; an episode is
# keyed by task and ref, not repo, so a cross-project offer is found; a
# ref offered twice keeps the rank it was first seen at; and the table is
# least-recently-used, so a long-lived task is not the first evicted
# (2026-09-29 audit, A6).
_OFFERS: OrderedDict[tuple, int] = OrderedDict()
_OFFERS_LIMIT = 5000


def _remember(kind: str, task_id: str | None, items: list[str], repo: str | None = None) -> None:
    for n, item in enumerate(items, 1):
        key = (kind, task_id, repo, item)
        if key in _OFFERS:
            _OFFERS.move_to_end(key)
            continue
        _OFFERS[key] = n
    while len(_OFFERS) > _OFFERS_LIMIT:
        _OFFERS.popitem(last=False)


def _rank_of(kind: str, task_id: str | None, item: str, repo: str | None = None) -> int | None:
    """1-based position of `item` when it was first put in front of this
    task, or None when it never was (or the offer is unknown)."""
    key = (kind, task_id, repo, item)
    rank = _OFFERS.get(key)
    if rank is not None:
        _OFFERS.move_to_end(key)
    return rank


def record_query(query: str, repo: str, refs: list[str], *, task_id: str | None = None,
                 legs: tuple[str, ...] | None = None, found_by: dict[str, list[str]] | None = None,
                 path: Path | None = None) -> None:
    """One search happened, and these are the refs it put in front of the
    model. Never raises."""
    _remember("episode", task_id, list(refs))
    _append({
        "ts": time.time(),
        "event": "query",
        "repo": repo,
        "task_id": task_id,
        # Truncated, not hashed: reading back why a search missed needs the
        # words, and a query is the model's own text, not anyone's secret.
        "query": (query or "")[:300],
        "legs": list(legs) if legs is not None else list(registered_legs()),
        "refs": list(refs)[:TOP_N],
        # Which leg put each of those refs there. `legs` alone says what was
        # running; this says what each one CONTRIBUTED, and the difference
        # is the whole question about the second leg: a vector leg whose
        # every hit full text also found has earned nothing, however many
        # searches it ran in.
        "found_by": {ref: list(found_by.get(ref, ())) for ref in list(refs)[:TOP_N]}
                    if found_by else {},
        "n_hits": len(refs),
    }, path)


def record_use(ref: str, repo: str, *, task_id: str | None = None, path: Path | None = None) -> None:
    """An episode a search offered was actually read. Never raises."""
    rank = _rank_of("episode", task_id, ref)
    _append({
        "ts": time.time(),
        "event": "use",
        "repo": repo,
        "task_id": task_id,
        "ref": ref,
        "rank": rank,
        # Separate from a null rank on purpose: "offered, rank unknown"
        # and "never offered" are different findings.
        "offered": rank is not None,
    }, path)


# The same two questions, asked of project memory instead of episodes: which
# sections did the prompt offer, and which of them did the task actually open.
# Memory sections and episode recall are the same wager made twice -- that an
# index is a cheaper way to carry knowledge than the knowledge itself -- and
# the wager is only settleable with a record of what was offered against what
# was used. An index entry that is offered a hundred times and never read is
# either a section nobody needs or, far more likely, an entry that does not
# say what it is for; both are fixes, and neither is visible without this.


def record_sections_offered(repo: str, offered: list[str], *, always: list[str] | None = None,
                            task_id: str | None = None, path: Path | None = None) -> None:
    """A prompt was built carrying this project's memory index. Never raises."""
    _remember("section", task_id, list(offered), repo=repo)
    _append({
        "ts": time.time(),
        "event": "memory_offered",
        "repo": repo,
        "task_id": task_id,
        "sections": list(offered),
        # Recorded separately because a pinned section cannot be "missed":
        # it was already in the prompt, so it never needed a read and its
        # absence from the read events means nothing.
        "always": list(always or []),
    }, path)


def record_section_read(slug: str, repo: str, *, task_id: str | None = None,
                        path: Path | None = None) -> None:
    """A section the index advertised was actually read. Never raises."""
    position = _rank_of("section", task_id, slug, repo=repo)
    _append({
        "ts": time.time(),
        "event": "memory_read",
        "repo": repo,
        "task_id": task_id,
        "section": slug,
        "position": position,
        "offered": position is not None,
    }, path)


def _append(entry: dict, path: Path | None) -> None:
    target = path or LOG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a") as f:
            f.write(json.dumps(entry) + "\n")
        _trim(target)
    except Exception as e:  # noqa: BLE001 -- telemetry must not break what it measures
        logger.debug("retrieval event not recorded: %s", e)


def _trim(path: Path) -> None:
    """Halve the file when it passes the cap, keeping the newest lines."""
    try:
        if path.stat().st_size <= MAX_BYTES:
            return
        with open(path, "rb") as f:
            data = f.read()
        keep = data[int(len(data) * (1 - _KEEP_FRACTION)):]
        keep = keep[keep.find(b"\n") + 1:] if b"\n" in keep else b""
        with open(path, "wb") as f:
            f.write(keep)
    except Exception as e:  # noqa: BLE001
        logger.debug("retrieval event log not trimmed: %s", e)
