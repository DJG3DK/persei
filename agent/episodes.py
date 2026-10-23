"""The one place an episode is written.

An episode is the structured record of how a task ended: the goal, the
outcome, what it cost, and -- when it went wrong -- why. It is not loaded
into any task's context. It is read by the consolidation agent, which
distills patterns across episodes into the semantic memory that IS loaded.

The value dict is composed here, in full, and written with one store.aput --
never through deepagents' StoreBackend.awrite, which rebuilds its FileData
document (content, encoding, two timestamps) from scratch and drops every
key it does not know. The embed_text key the vector leg needs is such a key,
and losing it is worse than a missing vector: langgraph emits no delete
against store_vectors on an update, so an episode stripped of embed_text
keeps the vector it had and goes on matching text it no longer contains. The
FileData shape is reproduced exactly, because consolidation.py's
_read_episode and every other reader go back through StoreBackend.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from deepagents.backends.utils import create_file_data
from langgraph.store.base import BaseStore

from agent import embeddings, episode_vectors, history_index
from agent.config import Config
from agent.deep_agent import EPISODES_ROUTE, episodes_namespace

logger = logging.getLogger("tektonix")


async def write_episode(store: BaseStore, config: Config, repo: str, record: dict[str, Any]) -> str:
    """Append-only: each episode gets its own timestamped key, never
    overwritten. namespace is (episodes, repo) -- shared across every task
    for this project, so the consolidation agent can list and read them all
    with one backend call.

    Returns the key it wrote, which is what a caller needs to refer to this
    episode later without guessing at the key shape.

    The store write comes first and the index second, in that order and
    never the other way: a stored episode that is not searchable is a gap a
    later sync closes, and a searchable episode that was never stored is a
    hit pointing at nothing.
    """
    namespace = episodes_namespace(repo)(None)
    path = f"{EPISODES_ROUTE}{record['timestamp']}-{uuid.uuid4().hex[:8]}.json"
    # The FileData half comes from deepagents' own helper, so the stored
    # timestamps keep the exact format every reader already parses; only the
    # embedding keys are ours.
    value = {
        **create_file_data(json.dumps(record, indent=2)),
        # `embed_text` is written on every installation, vector leg or not.
        # It costs nothing where there is no index -- langgraph only embeds
        # the keys an index config names, so every other writer in this
        # system, none of which carries this key, pays nothing either -- and
        # it is the text scripts/backfill_episode_embeddings.py embeds later
        # for episodes written before the feature was switched on.
        **episode_vectors.embed_fields(record),
    }
    if getattr(store, "index_config", None) is None:
        # The digest is a CLAIM THAT A VECTOR EXISTS for this text, and the
        # backfill trusts it: an episode whose digest matches is reported as
        # "already current" and never embedded. A store with no index
        # embeds nothing, so writing the digest here would mark every
        # episode written before the operator turned the leg on as done and
        # the backfill -- the one thing that would ever have given them a
        # vector -- would skip them forever. Silent and permanent, which is
        # why it is gated on the store rather than on the config.
        value.pop(episode_vectors.DIGEST_FIELD, None)
    try:
        await store.aput(namespace, path, value)
    except embeddings.EmbeddingError as e:
        # The embedding is made INSIDE aput, so an unreachable embedder
        # fails the write of a task that has already finished and spent its
        # money. That trade is never worth taking: store it with no vector
        # and let scripts/backfill_episode_embeddings.py pick it up, which
        # is exactly the case that script is written to be re-run for.
        #
        # Only EmbeddingError. A Postgres blip is not an embedder problem,
        # and catching everything here made the retry log name the wrong
        # cause on the one path an operator would be reading it -- while
        # hiding a store failure behind a second write that would fail too.
        logger.warning("episode %s stored without a vector, retrying without one: %s", path, e)
        value.pop(episode_vectors.EMBED_FIELD, None)
        value.pop(episode_vectors.DIGEST_FIELD, None)
        await store.aput(namespace, path, value, index=False)
    # Best-effort by contract, not by accident: index_episode swallows its
    # own failures. A task that has shipped must not be failed at the very
    # last step because a search index was unreachable -- and the nightly
    # sync picks this episode up anyway. `config` is what tells it whether
    # an installation with no index is a normal one or a misconfigured one.
    await history_index.index_episode(config, repo, path, record)
    return path
