"""Read-only SQL access to a project's own application database.

Why this exists as a host-side tool rather than just letting the agent run
`psql`/Prisma inside its sandbox: the sandbox container is on Docker's
default bridge, so `localhost` there is the container itself and the host's
Postgres is simply unreachable. Opening that up (`--network host`, or a
host-gateway route) would hand an agent-controlled container every
host-local service -- including the review service's control port, which is
unauthenticated precisely because "localhost only" was assumed to be the
boundary. This tool gives the agent the one capability it actually needed
-- "does this product exist, and what does its row look like" -- without
moving that boundary at all.

Found necessary 2026-08-24: a task asked to replace one product's hero
image searched the codebase, the seeds, the migrations and all 242 commits
for the product, found nothing, and stopped to ask the operator. It was
right that the product isn't in the code -- it's a catalog row in Postgres,
which the agent had no way to look at.

Safety, in layers, so no single one has to be perfect:
  1. Only SELECT/WITH statements are accepted, and only one per call.
  2. The query runs inside `BEGIN TRANSACTION READ ONLY` -- Postgres itself
     rejects any write, so this holds even if the check above is somehow
     talked past. That is the real guarantee; (1) exists to give a clear
     error rather than a confusing permission failure.
  3. A statement timeout, so a pathological query can't pin a connection.
  4. A hard row cap, so a `SELECT *` on a big table can't blow up the
     context window.
  5. The connection targets exactly the DSN configured for that one project.
     A Postgres connection reaches a single database, so this cannot read
     another project's data or the agent's own tables regardless of what
     the SQL says.

The DSN is read from the project's own live `.env` at call time rather than
copied into this agent's config -- one source of truth, and no second place
for a database credential to sit and go stale.
"""

import re
from pathlib import Path
from typing import Any, LiteralString, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from langchain_core.tools import BaseTool, tool

from agent.config import PROJECTS

MAX_ROWS = 200
STATEMENT_TIMEOUT_MS = 10_000
# Rendered cell width -- a description or image URL column can be enormous,
# and the agent needs to see WHICH row matched, not every byte of it.
MAX_CELL_CHARS = 300

_READ_ONLY_START = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


def _dsn_for(repo: str) -> str | None:
    """Reads DATABASE_URL out of the project's live checkout .env.

    Returns None when the project has no database configured, which is a
    normal state -- not every project this agent targets has one.
    """
    project = PROJECTS.get(repo) or {}
    env_rel = project.get("db_env_file")
    if not env_rel:
        return None
    env_path = Path(project["live"]) / env_rel
    try:
        text = env_path.read_text()
    except OSError:
        return None
    var = project.get("db_env_var", "DATABASE_URL")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(f"{var}="):
            continue
        value = line[len(var) + 1:].strip()
        # .env values are commonly quoted; psycopg wants the bare DSN.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return _strip_orm_params(value) or None
    return None


# Query params various ORMs bolt onto a DSN for their own pooling/schema
# handling. libpq doesn't know them and rejects the whole URI with
# "invalid URI query parameter", so a perfectly good Prisma DATABASE_URL
# would otherwise be unusable here purely because of a trailing
# `?schema=public`.
_ORM_ONLY_PARAMS = {"schema", "connection_limit", "pool_timeout", "pgbouncer", "socket_timeout", "sslaccept"}


def _strip_orm_params(dsn: str) -> str:
    parts = urlsplit(dsn)
    if not parts.query:
        return dsn
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in _ORM_ONLY_PARAMS]
    return urlunsplit(parts._replace(query=urlencode(kept)))


def _render(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    if not rows:
        return "(0 rows)"
    lines = [" | ".join(columns)]
    for row in rows:
        cells: list[str] = []
        for value in row:
            text = "NULL" if value is None else str(value)
            if len(text) > MAX_CELL_CHARS:
                text = text[:MAX_CELL_CHARS] + f"…(+{len(text) - MAX_CELL_CHARS} chars)"
            cells.append(text.replace("\n", " "))
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def make_project_db_tool(repo: str) -> BaseTool | None:
    """Returns a `query_db` tool bound to one project, or None when that
    project has no database configured (the tool is simply not offered,
    rather than offered and always failing)."""
    if not _dsn_for(repo):
        return None

    @tool
    async def query_db(sql: str) -> str:
        """Run a READ-ONLY SQL query against this project's application
        database and return the rows.

        Use this for anything about live application DATA rather than code:
        whether a product/user/order exists, what a row's real column values
        are, how many records match something. The codebase, seeds and
        migrations describe the SCHEMA; they do not tell you what is
        actually in the database right now, and grepping them for a specific
        product name will find nothing even when that product exists.

        Only a single SELECT (or WITH ... SELECT) is allowed -- no INSERT,
        UPDATE, DELETE, or DDL; the query runs in a read-only transaction,
        so a write is rejected by the database itself. Results are capped at
        200 rows, so add your own LIMIT and WHERE rather than selecting a
        whole table. Prefer naming the columns you need over `SELECT *`;
        wide text columns are truncated in the output.

        Example: SELECT id, title, status FROM "Product" WHERE title ILIKE '%cocktail%' LIMIT 20;
        """
        statement = sql.strip().rstrip(";").strip()
        if not statement:
            return "ERROR: empty query."
        if not _READ_ONLY_START.match(statement):
            return (
                "ERROR: only SELECT / WITH queries are allowed here. This tool is read-only; "
                "to change data, write real application code or a migration instead."
            )
        if ";" in statement:
            return "ERROR: only one statement per call -- remove the ';' and send a single SELECT."

        dsn = _dsn_for(repo)
        if not dsn:
            return f"ERROR: no database is configured for {repo!r}."

        try:
            async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=10) as conn:
                # READ ONLY is what actually enforces this, not the regex above.
                await conn.execute("SET TRANSACTION READ ONLY")
                # psycopg wants a literal query string. Both of these are
                # built at runtime on purpose: the first from a module
                # constant, the second is the model's own SQL, which is what
                # this tool exists to run (READ ONLY above is the guard).
                await conn.execute(cast(LiteralString, f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))
                cursor = await conn.execute(cast(LiteralString, statement))
                columns = [d.name for d in (cursor.description or [])]
                rows = await cursor.fetchmany(MAX_ROWS + 1)
        except Exception as e:  # noqa: BLE001 -- the model needs the real error text to fix its SQL
            return f"ERROR running query: {type(e).__name__}: {e}"

        truncated = len(rows) > MAX_ROWS
        body = _render(columns, rows[:MAX_ROWS])
        if truncated:
            body += f"\n\n(truncated at {MAX_ROWS} rows -- narrow the query with WHERE/LIMIT)"
        return body

    return query_db
