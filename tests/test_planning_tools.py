"""Unit tests for agent/tools/planning_tools.py -- the planning chat's own
tools (agent/planning_chat.py). web_search/browse_page need a real browser
and network access, so only their pure-function pieces and the two
filesystem-only tools (list_project_dir, read_project_file, save_plan) are
covered here; the tools themselves (including real cross-project reads) were
verified live against the real router/browser/repos during development.
"""

from agent.tools.planning_tools import make_planning_tools


def _write(tmp_path, rel_path: str, content: str = "") -> None:
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _tools(monkeypatch, projects: dict):
    monkeypatch.setattr("agent.tools.planning_tools.PROJECTS", projects)
    tools, plan_ref = make_planning_tools()
    by_name = {t.name: t for t in tools}
    return by_name, plan_ref


# ---------------------------------------------------------------------------
# list_project_dir / read_project_file -- cross-project by design, so every
# call names `repo` explicitly rather than being bound to one repo_root.
# ---------------------------------------------------------------------------


async def test_list_project_dir_lists_repo_root_by_default(tmp_path, monkeypatch):
    _write(tmp_path, "README.md")
    _write(tmp_path, "src/App.tsx")
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["list_project_dir"].ainvoke({"repo": "demo", "path": "."})
    assert "README.md" in result
    assert "src/" in result


async def test_list_project_dir_lists_a_subdirectory(tmp_path, monkeypatch):
    _write(tmp_path, "src/App.tsx")
    _write(tmp_path, "src/index.css")
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["list_project_dir"].ainvoke({"repo": "demo", "path": "src"})
    assert "App.tsx" in result
    assert "index.css" in result


async def test_list_project_dir_refuses_path_escape(tmp_path, monkeypatch):
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["list_project_dir"].ainvoke({"repo": "demo", "path": "../../etc"})
    assert result.startswith("ERROR:")


async def test_list_project_dir_on_missing_path_returns_error(tmp_path, monkeypatch):
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["list_project_dir"].ainvoke({"repo": "demo", "path": "nope"})
    assert result.startswith("ERROR:")
    assert "does not exist" in result


async def test_list_project_dir_on_a_file_not_a_directory_returns_error(tmp_path, monkeypatch):
    _write(tmp_path, "README.md")
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["list_project_dir"].ainvoke({"repo": "demo", "path": "README.md"})
    assert result.startswith("ERROR:")
    assert "not a directory" in result


async def test_list_project_dir_on_empty_directory(tmp_path, monkeypatch):
    (tmp_path / "empty").mkdir()
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["list_project_dir"].ainvoke({"repo": "demo", "path": "empty"})
    assert result == "(empty directory)"


async def test_list_project_dir_rejects_unknown_repo(tmp_path, monkeypatch):
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["list_project_dir"].ainvoke({"repo": "not-configured", "path": "."})
    assert result.startswith("ERROR:")
    assert "unknown repo" in result


async def test_read_project_file_reads_from_the_named_repo(tmp_path, monkeypatch):
    _write(tmp_path, "notes.md", "hello from demo")
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": "notes.md"})
    assert result == "hello from demo"


async def test_read_project_file_can_reach_a_second_configured_project(tmp_path, monkeypatch):
    """The whole point of these tools: a planning session about one project
    can still read a DIFFERENT configured project for comparison/reference."""
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    _write(repo_a, "notes.md", "repo a content")
    _write(repo_b, "notes.md", "repo b content")
    by_name, _ = _tools(monkeypatch, {"repo-a": {"sandbox": str(repo_a)}, "repo-b": {"sandbox": str(repo_b)}})
    a = await by_name["read_project_file"].ainvoke({"repo": "repo-a", "path": "notes.md"})
    b = await by_name["read_project_file"].ainvoke({"repo": "repo-b", "path": "notes.md"})
    assert a == "repo a content"
    assert b == "repo b content"


async def test_read_project_file_rejects_unknown_repo(tmp_path, monkeypatch):
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke({"repo": "not-configured", "path": "notes.md"})
    assert result.startswith("ERROR:")
    assert "unknown repo" in result


async def test_read_project_file_on_missing_file_returns_error(tmp_path, monkeypatch):
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": "nope.md"})
    assert result.startswith("ERROR:")


async def test_read_project_file_through_a_worktree_git_pointer_returns_an_error(tmp_path, monkeypatch):
    """The live failure this guards: every sandbox is a git WORKTREE, whose
    `.git` is a one-line pointer FILE, so `.git/HEAD` resolves THROUGH a file
    and raises NotADirectoryError. Only FileNotFoundError was caught, so it
    escaped the tool -- and langgraph re-raises anything that isn't a
    ToolInvocationError, which killed the entire planning turn instead of the
    one tool call."""
    _write(tmp_path, ".git", "gitdir: /home/my-service/.git/worktrees/my-service\n")
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": ".git/HEAD"})
    assert result.startswith("ERROR:")
    assert "worktree" in result
    # Never leak the host sandbox root to the model (see _resolve in files.py).
    assert str(tmp_path) not in result


async def test_read_project_file_on_a_directory_returns_an_error(tmp_path, monkeypatch):
    _write(tmp_path, "src/App.tsx")
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": "src"})
    assert result.startswith("ERROR:")
    assert "list_project_dir" in result


async def test_an_unexpected_tool_failure_returns_a_string_instead_of_raising(tmp_path, monkeypatch):
    """Belt to the OSError braces above: whatever a tool body fails on next,
    it must come back as a tool RESULT, not an exception that ends the turn."""
    def _explode(*_a, **_kw):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr("agent.tools.planning_tools.read_file", _explode)
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": "notes.md"})
    assert result.startswith("ERROR:")
    assert "something nobody predicted" in result


# ---------------------------------------------------------------------------
# read_project_file paging -- the planning agent has no bash, so this tool is
# its ONLY route into a repo. Without offset/limit a file past the inline cap
# was unreachable beyond its first 40k chars, and re-reading returned the
# identical truncated text (live 2026-08-27, on a 74_703-char strategy file).
# ---------------------------------------------------------------------------


def _big_file(tmp_path, lines: int = 4000) -> int:
    body = "\n".join(f"line {i} " + "x" * 40 for i in range(1, lines + 1))
    _write(tmp_path, "big.js", body)
    return lines


async def test_a_large_file_still_returns_its_beginning(tmp_path, monkeypatch):
    _big_file(tmp_path)
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": "big.js"})
    assert "line 1 " in result


async def test_a_truncated_read_says_repeating_it_is_pointless_and_how_to_page(tmp_path, monkeypatch):
    _big_file(tmp_path)
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": "big.js"})
    assert "TRUNCATED" in result
    assert "SAME" in result
    assert "offset" in result and "limit" in result


async def test_offset_and_limit_reach_past_the_inline_cap(tmp_path, monkeypatch):
    """The whole point: content the plain read cannot show must be reachable."""
    total = _big_file(tmp_path)
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    plain = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": "big.js"})
    assert f"line {total} " not in plain
    tail = await by_name["read_project_file"].ainvoke(
        {"repo": "demo", "path": "big.js", "offset": total - 5, "limit": 20}
    )
    assert f"line {total} " in tail


async def test_a_paged_slice_is_line_numbered_and_reports_what_remains(tmp_path, monkeypatch):
    total = _big_file(tmp_path)
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke(
        {"repo": "demo", "path": "big.js", "offset": 10, "limit": 3}
    )
    assert result.startswith("10\tline 10 ")
    # limit=3 is widened to the 500-line floor (_READ_MIN_WINDOW): a planner
    # paging a big file in 100-line windows was the 2026-09-09 cost sink.
    assert f"[lines 10-509 of {total}" in result
    assert f"{total - 509} more after this]" in result


async def test_the_last_slice_does_not_claim_more_is_coming(tmp_path, monkeypatch):
    total = _big_file(tmp_path, lines=50)
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke(
        {"repo": "demo", "path": "big.js", "offset": 45, "limit": 100}
    )
    assert f"[lines 45-{total} of {total}]" in result
    assert "more after this" not in result


async def test_an_offset_past_the_end_says_so_instead_of_returning_nothing(tmp_path, monkeypatch):
    total = _big_file(tmp_path, lines=20)
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke(
        {"repo": "demo", "path": "big.js", "offset": 500, "limit": 10}
    )
    assert f"has {total} lines" in result


async def test_a_small_file_is_returned_verbatim_with_no_paging_furniture(tmp_path, monkeypatch):
    """Paging must not change what a normal read has always returned."""
    _write(tmp_path, "notes.md", "hello from demo")
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": "notes.md"})
    assert result == "hello from demo"


# ---------------------------------------------------------------------------
# save_plan
# ---------------------------------------------------------------------------


async def test_save_plan_writes_into_the_shared_ref(monkeypatch):
    by_name, plan_ref = _tools(monkeypatch, {})
    assert plan_ref["markdown"] is None
    await by_name["save_plan"].ainvoke({"markdown": "# Plan\n\nDo the thing."})
    assert plan_ref["markdown"] == "# Plan\n\nDo the thing."


async def test_save_plan_replaces_the_previous_draft(monkeypatch):
    by_name, plan_ref = _tools(monkeypatch, {})
    await by_name["save_plan"].ainvoke({"markdown": "draft one"})
    await by_name["save_plan"].ainvoke({"markdown": "draft two"})
    assert plan_ref["markdown"] == "draft two"


# ---------------------------------------------------------------------------
# own-space redirect -- wrong tool, right correction
# ---------------------------------------------------------------------------


async def test_an_own_space_path_redirects_to_read_file(tmp_path, monkeypatch):
    """Live 2026-08-28: the prompt says to read the codebase map with built-in
    read_file; the model aimed read_project_file at it and the escape-guard's
    "paths must be RELATIVE" reply sent it hunting for a relative spelling of
    a file that was never in the repo. The error now names the right tool."""
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    for path in ("/skills/codebase-map/SKILL.md", "/memories/AGENTS.md", "/org-memory/AGENTS.md"):
        result = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": path})
        assert result.startswith("ERROR:") and "read_file" in result and "YOUR OWN file space" in result
    result = await by_name["list_project_dir"].ainvoke({"repo": "demo", "path": "/skills/"})
    assert "YOUR OWN file space" in result


async def test_a_relative_skills_dir_in_the_repo_is_still_readable(tmp_path, monkeypatch):
    """A repo may legitimately contain its own top-level skills/ directory --
    only ABSOLUTE own-space prefixes redirect."""
    _write(tmp_path, "skills/notes.md", "repo skill notes")
    by_name, _ = _tools(monkeypatch, {"demo": {"sandbox": str(tmp_path)}})
    result = await by_name["read_project_file"].ainvoke({"repo": "demo", "path": "skills/notes.md"})
    assert result == "repo skill notes"


# ---------------------------------------------------------------------------
# create_project -- a proposal, never a creation. The tool records what the
# operator confirmed into plan_ref; the server puts a Confirm card on the
# session and creates the project only when an admin answers it.
# ---------------------------------------------------------------------------


def _admin_tools(monkeypatch, projects: dict):
    monkeypatch.setattr("agent.tools.planning_tools.PROJECTS", projects)
    tools, plan_ref = make_planning_tools(is_admin=True, actor="admin@example.com")
    return {t.name: t for t in tools}, plan_ref


def test_create_project_is_offered_to_everyone_by_default(monkeypatch):
    """Offered unconditionally (EASY and HARD must see identical tool sets);
    the gate is inside the tool, not in the list."""
    by_name, _ = _tools(monkeypatch, {})
    assert "create_project" in by_name


def test_create_project_refuses_a_non_admin(monkeypatch):
    by_name, plan_ref = _tools(monkeypatch, {})  # is_admin defaults False
    result = by_name["create_project"].invoke({"name": "my-app"})
    assert result.startswith("ERROR:")
    assert "admin-only" in result
    assert "new_project" not in plan_ref, "a refused call must record nothing"


def test_create_project_refuses_a_bad_name_with_the_servers_wording(monkeypatch):
    by_name, plan_ref = _admin_tools(monkeypatch, {})
    result = by_name["create_project"].invoke({"name": ".hidden"})
    assert result.startswith("ERROR:")
    assert "must start with a letter or digit" in result
    assert "new_project" not in plan_ref


def test_create_project_refuses_a_duplicate_name(monkeypatch):
    by_name, plan_ref = _admin_tools(monkeypatch, {"shop": {"sandbox": "/tmp/x"}})
    result = by_name["create_project"].invoke({"name": "shop"})
    assert result.startswith("ERROR:")
    assert "'shop' is already configured" in result
    assert "new_project" not in plan_ref


def test_create_project_records_the_proposal_and_points_at_the_confirm_card(monkeypatch):
    by_name, plan_ref = _admin_tools(monkeypatch, {"shop": {"sandbox": "/tmp/x"}})
    result = by_name["create_project"].invoke(
        {"name": " my-app ", "description": " A store front ", "github": True})
    assert plan_ref["new_project"] == {
        "name": "my-app", "description": "A store front", "github": True,
        "proposed_by": "admin@example.com",
    }
    assert not result.startswith("ERROR:")
    assert "Confirm" in result
    assert "Do not call create_project again" in result
    assert "as if the project already exists" in result


async def test_web_search_spend_is_attributed_to_the_session(monkeypatch):
    """2026-10-22 audit, A23: the search's ledger line had no owner, so a
    session's total missed what its searches cost."""
    from agent.tools import web_search as ws

    seen = []

    async def search(query, num_results=6, *, metadata=None):
        seen.append(metadata)
        return "results"

    monkeypatch.setattr(ws, "web_search", search)
    tools, _ = make_planning_tools(session_id="s-123")
    tool = {t.name: t for t in tools}["web_search"]
    assert await tool.ainvoke({"query": "q"}) == "results"
    assert seen == [{"agent_session_id": "s-123"}]

    tools, _ = make_planning_tools()
    await {t.name: t for t in tools}["web_search"].ainvoke({"query": "q"})
    assert seen[-1] is None, "no session, no tag"
