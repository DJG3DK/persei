"""Cartographer: inventory determinism, hash stability, and the skip marker.

These are the properties that make scheduling it cheap. If the hash moves when
nothing structural changed, every run calls the model and the marker is
pointless
if it does NOT move when structure changes, maps silently go stale.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.cartographer import build_inventory, inventory_hash


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n")
    (tmp_path / "src" / "util.py").write_text("def f(): pass\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_x(): pass\n")
    (tmp_path / "package.json").write_text(json.dumps({"name": "demo", "scripts": {"test": "pytest"}}))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x" * 5000)
    return tmp_path


def test_inventory_skips_dependency_dirs(tmp_path):
    inv = build_inventory("demo", str(_repo(tmp_path)))
    assert not any("node_modules" in p for p in inv["tree"]), \
        "node_modules must never enter the inventory — it would dominate the language histogram"
    assert inv["languages"].get("Python") == 3


def test_inventory_finds_tests_and_manifests(tmp_path):
    inv = build_inventory("demo", str(_repo(tmp_path)))
    assert any("test_main.py" in t for t in inv["test_files"])
    assert "package.json" in inv["manifests"]
    assert "pytest" in inv["manifests"]["package.json"]


def test_hash_is_stable_across_runs(tmp_path):
    root = str(_repo(tmp_path))
    assert inventory_hash(build_inventory("demo", root)) == inventory_hash(build_inventory("demo", root))


def test_hash_moves_when_structure_changes(tmp_path):
    root = _repo(tmp_path)
    before = inventory_hash(build_inventory("demo", str(root)))
    (root / "src" / "new_module.py").write_text("x = 1\n")
    after = inventory_hash(build_inventory("demo", str(root)))
    assert before != after, "a new source file must invalidate the map"


def test_hash_ignores_commit_churn(tmp_path):
    """recent_commits and hot_files change on every commit. If they fed the
    hash, every commit would trigger a full re-map — the marker would save
    nothing."""
    root = str(_repo(tmp_path))
    inv = build_inventory("demo", root)
    base = inventory_hash(inv)
    inv2 = dict(inv)
    inv2["recent_commits"] = "totally different subjects"
    inv2["hot_files"] = {"src/main.py": 99}
    assert inventory_hash(inv2) == base


# ---------------------------------------------------------------------------
# recent-changes: the model-free changelog skill rebuilt whenever HEAD moves.
# ---------------------------------------------------------------------------

import subprocess

from agent.cartographer import CHANGES_COMMITS, build_recent_changes


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True)


def _repo_with_history(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text("1")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "feat: add a\n\nBecause the bot needed an a.\nSecond body line.")
    (tmp_path / "src" / "b.js").write_text("2")
    (tmp_path / "src" / "a.js").write_text("11")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "fix: b and a")
    return tmp_path


def test_recent_changes_carries_subjects_bodies_and_files(tmp_path):
    head, md = build_recent_changes("demo", str(_repo_with_history(tmp_path)))
    assert len(head) == 40
    assert md.startswith("# Recent changes in demo")
    assert "fix: b and a" in md and "feat: add a" in md
    assert "Because the bot needed an a." in md, "the commit body is the WHY -- it must survive"
    assert "`src/a.js`" in md and "`src/b.js`" in md
    assert "`src/a.js` (2 commits)" in md, "most-touched files are counted across the window"
    assert md.index("fix: b and a") < md.index("feat: add a"), "newest first"


def test_recent_changes_is_empty_without_git(tmp_path):
    assert build_recent_changes("demo", str(tmp_path)) == ("", "")


def test_recent_changes_window_is_bounded(tmp_path):
    root = _repo_with_history(tmp_path)
    for i in range(CHANGES_COMMITS + 5):
        (root / "src" / "a.js").write_text(str(i) * 3)
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", f"chore: bump {i}")
    _, md = build_recent_changes("demo", str(root))
    assert md.count("\n### ") == CHANGES_COMMITS
    assert "feat: add a" not in md


# --- the in-process run stays off the event loop -----------------------------

@pytest.mark.asyncio
async def test_the_inventory_walk_does_not_starve_the_loop(tmp_path, monkeypatch):
    """Since the cartographer runs inside the API process (agent/jobs.py),
    a synchronous build_inventory on the loop stalled every request and
    WebSocket for the length of each project's walk (2026-09-29)."""
    import asyncio
    import time

    from langgraph.store.memory import InMemoryStore

    from agent import cartographer as cart
    from agent.cartographer import MAP_MARKER_PATH, StoreBackend, project_namespace

    inv = {"file_count": 1, "languages": {"py": 1}, "top_directories": [], "manifests": [],
           "test_files": [], "docs": [], "tree": ["a.py"]}

    def slow_inventory(repo, root):
        time.sleep(0.3)          # a walk of a big tree, on whatever thread it is called from
        return inv

    async def quiet(*a, **k):
        return {}

    monkeypatch.setattr(cart, "build_inventory", slow_inventory)
    monkeypatch.setattr(cart, "refresh_recent_changes", quiet)
    monkeypatch.setattr(cart, "refresh_freshness", quiet)
    monkeypatch.setitem(cart.PROJECTS, "demo", {"live": str(tmp_path), "sandbox": str(tmp_path)})
    store = InMemoryStore()
    # The marker already matches, so the run ends before any model call.
    await StoreBackend(namespace=project_namespace("demo"), store=store).awrite(
        MAP_MARKER_PATH, cart.inventory_hash(inv))

    # A request handler that wants the loop every 10ms while the walk runs.
    # On a blocked loop one of its gaps is the whole walk.
    gaps: list[float] = []
    async def ticker(until):
        last = time.monotonic()
        while not until.done():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    run = asyncio.create_task(cart.run_cartographer(object(), "demo", store))
    await ticker(run)
    result = await run
    assert result["mapped"] is False and result["reasoning"].startswith("repo structure unchanged")
    assert max(gaps) < 0.15, f"the loop stalled for {max(gaps):.2f}s during a 300ms inventory walk"


@pytest.mark.asyncio
async def test_a_block_list_reply_is_read_as_text(tmp_path, monkeypatch):
    """A provider can answer with a list of content blocks rather than a
    string; `.strip()` on that list raised and no map was ever written."""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage
    from langgraph.store.memory import InMemoryStore

    from agent import cartographer as cart

    inv = {"file_count": 1, "languages": {"py": 1}, "top_directories": [], "manifests": [],
           "test_files": [], "docs": [], "tree": ["a.py"]}

    async def quiet(*a, **k):
        return {}

    class _Model:
        async def ainvoke(self, messages):
            return AIMessage(content=[{"type": "text", "text": "  # Map\nall in a.py  "}])

    seeded: list[str] = []

    async def fake_seed(repo, store, name, description, content):
        seeded.append(content)

    monkeypatch.setattr(cart, "build_inventory", lambda repo, root: inv)
    monkeypatch.setattr(cart, "_render_inventory", lambda inv: "inventory")
    monkeypatch.setattr(cart, "refresh_recent_changes", quiet)
    monkeypatch.setattr(cart, "refresh_freshness", quiet)
    monkeypatch.setattr(cart, "init_chat_model", lambda *a, **k: _Model())
    monkeypatch.setattr(cart, "seed_skill", fake_seed)
    monkeypatch.setitem(cart.PROJECTS, "demo", {"live": str(tmp_path), "sandbox": str(tmp_path)})
    config = SimpleNamespace(router_base_url="http://router.invalid", router_api_key="k")

    result = await cart.run_cartographer(config, "demo", InMemoryStore())

    assert result["mapped"] is True
    assert seeded and seeded[0].endswith("# Map\nall in a.py")
