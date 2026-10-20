#!/usr/bin/env python3
"""Run SWE-bench Verified through Persei and grade it with the official harness.

    scripts/run_swebench.py --instances psf__requests-1142 django__django-10097
    scripts/run_swebench.py --sample 50 --seed 1 --parallel 3
    scripts/run_swebench.py --all --parallel 4           # the advertisable number

A run leaves everything in logs/swebench/<run-id>/: predictions.jsonl (the
submission), summary.json (rewritten after every task: per task the outcome,
cost, models, the reviewer's verdict and record, the harness's own note on
an unresolved task; for the run the runtime settings, `coder_reasoning` and
the host peaks), the harness's report and grading.log, trajectories/<id>.json
(the agent's whole conversation), host.jsonl (the box and the router once a
minute, agent/evals/host_metrics.py) and reviewer/ (the pair's history and
state, kept out of the batch's temporary root).

evals/SWEBENCH.md says what makes the result a SWE-bench result and what
must not change for it to stay one. Import order matters here for the same
reason it does in run_evals.py: nothing may import agent.config before the
run's own projects.json is in AGENT_PROJECTS_JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# No LangSmith tracing for a benchmark run. .env turns it on for the server;
# here it serialised every node and model call's whole state on the runner's
# one core (at 100% with four tasks), and sent benchmark traces into the
# production project. Set before agent.config loads .env, which never
# overrides a variable already set.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from agent import paths  # noqa: E402
from agent import tool_events  # noqa: E402
from agent.evals import host_metrics  # noqa: E402
from agent.evals import reviewer as ev_reviewer  # noqa: E402
from agent.evals import swebench as sb  # noqa: E402

RUNS = paths.REPO_ROOT / "logs" / "swebench"


def _args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    pick = p.add_mutually_exclusive_group(required=True)
    pick.add_argument("--grade-only", metavar="RUN_ID",
                      help="grade an earlier run's predictions again, without re-running anything")
    pick.add_argument("--instances", nargs="+", metavar="ID")
    pick.add_argument("--sample", type=int, metavar="N", help="a seeded random sample of N tasks")
    pick.add_argument("--all", action="store_true", help="all 500 -- the number that can be advertised")
    p.add_argument("--gold-check", action="store_true",
                   help="grade the dataset's own reference fixes instead of running the agent: "
                        "which tasks can be solved at all in the official images")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--parallel", type=int, default=1, help="tasks at once (default 1)")
    p.add_argument("--budget", type=float, default=3.0, help="per-task spending cap, $ (default 3)")
    p.add_argument("--ceiling", type=float, default=100.0, help="stop starting tasks past this total, $")
    p.add_argument("--grade-workers", type=int, default=4)
    p.add_argument("--run-id", default=None)
    p.add_argument("--notes", default="")
    p.add_argument("--skip-grade", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep the task repositories afterwards")
    p.add_argument("--batch-size", type=int, default=20,
                   help="tasks per batch; each batch's images and repositories are deleted once it "
                        "is graded (default 20, about 60 GB of images at a time)")
    p.add_argument("--max-disk-pct", type=float, default=80.0,
                   help="never start a batch or pull an image with the disk this full (default 80)")
    p.add_argument("--shard", default=None, metavar="K/N",
                   help="run only every N-th selected task, starting at the K-th (1-based), so N runner "
                        "processes share one selection: one process's event loop and SQLite file per few "
                        "tasks, and each shard its own --run-id")
    p.add_argument("--task-timeout-min", type=int, default=180,
                   help="a guard against a hung task, not a limit on the work: SWE-bench sets no time "
                        "limit, and --budget is what bounds a task (default 180)")
    p.add_argument("--keep-images", action="store_true", help="do not delete a batch's images afterwards")
    p.add_argument("--coder-reasoning", choices=("on", "off"), default="on",
                   help="off: the coder seat runs with its chain of thought disabled (CODER_REASONING=off "
                        "for this process only); recorded in the summary")
    return p.parse_args(argv)


async def _save_trajectory(checkpointer, task_id: str, repo: str, generations: int, out: Path) -> None:
    """The agent's own conversation for a task -- every message and tool call,
    the coordinator's and each subagent's, across fresh-thread generations --
    written beside the predictions. The leaderboard asks for trajectories, and
    the run's store is a temporary file deleted at the end."""
    from langchain_core.messages import messages_to_dict  # noqa: PLC0415

    from agent.nodes.work import inner_thread_config  # noqa: PLC0415
    threads = []
    for gen in range(generations + 1):
        cfg = inner_thread_config(task_id, repo, gen)
        tuples = [t async for t in checkpointer.alist({"configurable": cfg["configurable"]})]
        for ns, msgs in conversations(tuples).items():
            threads.append({"generation": gen, "namespace": ns or "coordinator",
                            "messages": messages_to_dict(msgs)})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"task_id": task_id, "threads": threads}, indent=1, default=str))


def conversations(tuples) -> dict[str, list]:
    """Every message each namespace of a thread ever held, in order.

    Rebuilt from the checkpoints' writes, not read off the last checkpoint:
    the agent's `messages` is a DeltaChannel, whose checkpoint holds only a
    marker (every trajectory of the first runs was empty), and summarization
    removes old messages from the live state that a trajectory must keep."""
    from langchain_core.messages import BaseMessage, RemoveMessage  # noqa: PLC0415
    by_ns: dict[str, dict] = {}
    for t in sorted(tuples, key=lambda t: t.config["configurable"].get("checkpoint_id", "")):
        seen = by_ns.setdefault(t.config["configurable"].get("checkpoint_ns", ""), {})
        for _task, channel, value in t.pending_writes or []:
            if channel != "messages":
                continue
            for m in value if isinstance(value, list) else [value]:
                if isinstance(m, BaseMessage) and not isinstance(m, RemoveMessage) and m.id not in seen:
                    seen[m.id] = m
    return {ns: list(msgs.values()) for ns, msgs in by_ns.items() if msgs}


def _router_ledger(task_id: str) -> tuple[dict, float]:
    """Which models the router served this task, and what it billed -- from
    the router's own ledger, the only spend figure that matches the invoice.
    The task's own cost_so_far is lost when a task ends in an exception."""
    ledger = paths.REPO_ROOT / "services" / "model-router" / "logs" / "routing.jsonl"
    counts: dict = {}
    billed = 0.0
    try:
        with ledger.open() as fh:
            for line in fh:
                if task_id not in line:
                    continue
                d = json.loads(line)
                if d.get("task_id") == task_id:
                    key = f"{d.get('alias')} -> {d.get('routed_model')}"
                    counts[key] = counts.get(key, 0) + 1
                    billed += float(d.get("cost") or 0.0)
    except (OSError, ValueError):
        pass
    return counts, billed


def shard(instances: list[dict], spec: str | None) -> list[dict]:
    """Every N-th task starting at the K-th, for spec "K/N"; all when None.
    The shards of one selection are disjoint and together are all of it."""
    if not spec:
        return instances
    k, _, n = spec.partition("/")
    k, n = int(k), int(n)
    if not 1 <= k <= n:
        raise SystemExit(f"--shard {spec}: K must be between 1 and N")
    return instances[k - 1::n]


def publish_projects(path: Path, projects: dict) -> Path:
    """This batch's projects, where the agent reads them.

    One file for the whole run, rewritten per batch. agent.config fixes the
    path it reads when it is first imported; each batch writing into its own
    temporary folder meant the second batch reloaded a file that had been
    deleted with the first, fell back to the example projects, and crashed
    on its first task (2026-09-24, 20 tasks in)."""
    path.write_text(json.dumps({"projects": projects}, indent=1))
    os.environ["AGENT_PROJECTS_JSON"] = str(path)
    if "agent.config" in sys.modules:
        import agent.config as cfg  # noqa: PLC0415
        if Path(cfg._PROJECTS_CONFIG_PATH) != path:
            raise RuntimeError(f"the agent reads projects from {cfg._PROJECTS_CONFIG_PATH}, not {path}")
        cfg.reload_projects()
        missing = [name for name in projects if name not in cfg.PROJECTS]
        if missing:
            raise RuntimeError(f"projects not visible to the agent after reload: {missing[:3]}")
    return path


def point_agent_at_reviewer(overrides: dict[str, str]) -> None:
    """This batch's reviewer pair, where the agent calls it.

    agent/tools/review_gate.py reads its ports once, when first imported, so
    the environment alone only reaches the FIRST batch: the second batch's
    tasks kept calling the first batch's pair, stopped with it, and waited
    on a reviewer that was gone (2026-09-24). The module's own values are
    set too, and its cached project checks dropped."""
    os.environ.update(overrides)
    gate = sys.modules.get("agent.tools.review_gate")
    if gate is not None:
        gate.REVIEW_SERVICE_PORT = int(overrides["REVIEW_SERVICE_PORT"])
        gate.REVIEW_CONTROL_PORT = int(overrides["REVIEW_CONTROL_PORT"])
        gate._CHECKS_CACHE.clear()


def review_record(review: dict | None) -> dict | None:
    """The reviewer's whole verdict for the summary -- text and findings, not
    just the verdict word. 2026-09-25: only `review_verdict` was kept, and the
    pair's own files went with the run's temporary root, so "what did the
    reviewer say" had no answer afterwards. `baseline` is dropped: a cache of
    which checks fail on base, not a review."""
    if not review:
        return None
    return {k: v for k, v in review.items() if k != "baseline"}


def keep_reviewer_files(src: Path, dst: Path) -> None:
    """The batch's reviewer state, copied out of the temporary root before it
    is deleted: history.jsonl and reviewer.log appended (one run, many
    batches), state.json merged per project."""
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("history.jsonl", "reviewer.log"):
        f = src / name
        if f.is_file():
            with (dst / name).open("ab") as out, f.open("rb") as inp:
                shutil.copyfileobj(inp, out)
    state = src / "state.json"
    if state.is_file():
        try:
            merged = json.loads((dst / "state.json").read_text()) if (dst / "state.json").is_file() else {}
            merged.update(json.loads(state.read_text()))
            (dst / "state.json").write_text(json.dumps(merged, indent=1))
        except ValueError:
            shutil.copy2(state, dst / "state.json")


def disk_used_pct(path: str = "/var/lib/docker") -> float:
    """How full the disk the images land on is, the way df reports it."""
    try:
        u = shutil.disk_usage(path)
    except OSError:
        u = shutil.disk_usage("/")
    return 100.0 * u.used / (u.used + u.free)


async def _run(args, instances: list[dict], root: Path, run_dir: Path, spent_before: float = 0.0,
               on_result=None) -> dict:
    # --- 1. every task's repository, out of its official image -- each pull
    # only while the disk has room for it.
    mfs, skipped = {}, {}
    for inst in instances:
        if disk_used_pct() >= args.max_disk_pct:
            skipped[inst["instance_id"]] = {"outcome": "not_run",
                                            "reason": f"disk at {disk_used_pct():.0f}% (limit {args.max_disk_pct:.0f}%)"}
            continue
        print(f"setting up {inst['instance_id']} ...", flush=True)
        try:
            mfs[inst["instance_id"]] = sb.materialize(inst, root / "work")
        except (sb.SetupError, subprocess.SubprocessError, OSError) as e:
            # One task that cannot be set up is that task's failure, not the
            # run's: it stays in the total, unresolved, with the reason.
            print(f"  {inst['instance_id']}: SETUP FAILED -- {e}", flush=True)
            skipped[inst["instance_id"]] = {"outcome": "setup_error", "reason": str(e)[:500]}
    instances = [i for i in instances if i["instance_id"] in mfs]
    if not instances:
        return {"results": skipped, "runtime_settings": {}}
    projects_json = publish_projects(run_dir / "projects.json", {
        mfs[i["instance_id"]]["name"]: sb.project_entry(i, mfs[i["instance_id"]]) for i in instances})

    # --- 2. an isolated reviewer pair and store, production's settings
    rev = await ev_reviewer.start(projects_json, root / "reviewer")
    point_agent_at_reviewer(rev.env_overrides)
    results: dict[str, dict] = {}
    predictions = run_dir / "predictions.jsonl"
    try:
        from agent.config import load_config  # noqa: PLC0415
        from agent.evals.runner import _final_state, _read_outcome, eval_config  # noqa: PLC0415
        from agent.graph import open_checkpointer, open_store  # noqa: PLC0415
        from agent.outer_graph import build_outer_graph  # noqa: PLC0415
        from agent.outer_state import initial_state  # noqa: PLC0415
        from agent import runtime_settings as rs, workspaces  # noqa: PLC0415
        from run_evals import live_runtime_settings  # noqa: PLC0415

        live = load_config()
        print(f"runtime settings: {await live_runtime_settings(live, open_store)}", flush=True)
        config = eval_config(live, root)
        spent = spent_before           # the ceiling is the whole run's, across batches
        gate = asyncio.Semaphore(max(1, args.parallel))

        async with open_checkpointer(config) as checkpointer, open_store(config) as store:
            graph = build_outer_graph(config, checkpointer, store).compile(checkpointer=checkpointer, store=store)

            async def one(inst: dict) -> None:
                nonlocal spent
                iid, mf = inst["instance_id"], mfs[inst["instance_id"]]
                async with gate:
                    if spent + args.budget > args.ceiling:
                        results[iid] = {"outcome": "not_run", "reason": "ceiling"}
                        return
                    task_id, started = str(uuid.uuid4()), time.monotonic()
                    state = initial_state(task_id=task_id, goal=sb.goal_for(inst), repo=mf["name"],
                                          budget_usd=args.budget, require_merge_review=True,
                                          auto_approve_commands=True)
                    cfg = {"configurable": {"thread_id": task_id},
                           "metadata": {"task_id": task_id, "repo": mf["name"], "swebench": iid},
                           "tags": ["swebench", mf["name"]]}
                    error = None
                    try:
                        await asyncio.wait_for(graph.ainvoke(state, cfg), timeout=args.task_timeout_min * 60)
                    except Exception as e:  # noqa: BLE001 -- one task must not stop the run
                        error = f"{type(e).__name__}: {e}"[:500]
                    final = await _final_state(graph, cfg)
                    outcome, reason = ("error", error) if error else _read_outcome(final)
                    try:
                        await _save_trajectory(checkpointer, task_id, mf["name"],
                                               int(final.get("inner_thread_generation") or 0),
                                               run_dir / "trajectories" / f"{iid}.json")
                    except Exception as e:  # noqa: BLE001 -- a missing log must not lose the prediction
                        print(f"  {iid}: trajectory not saved: {e}", flush=True)
                    models, billed = _router_ledger(task_id)
                    cost = billed or float(final.get("cost_so_far") or 0.0)
                    spent += cost
                    try:
                        ws = Path(workspaces.task_workspace_path(mf["name"], task_id))
                    except Exception:  # noqa: BLE001 -- the prediction still comes from the template
                        ws = Path(mf["sandbox"])
                    try:
                        patch = sb.prediction_patch(ws if (ws / ".git").exists() else mf["sandbox"], mf["base"], mf["live"])
                    except sb.SetupError as e:
                        patch, reason = "", f"{reason or ''} | patch: {e}"
                    with predictions.open("a") as fh:
                        fh.write(json.dumps({"instance_id": iid, "model_name_or_path": sb.MODEL_NAME,
                                             "model_patch": patch}) + "\n")
                    review = final.get("review_gate_result") or {}
                    results[iid] = {"task_id": task_id, "outcome": outcome, "reason": reason, "cost_usd": round(cost, 4),
                                    "duration_s": round(time.monotonic() - started), "patch_bytes": len(patch),
                                    "review_verdict": review.get("verdict"), "review": review_record(review),
                                    "models": models}
                    print(f"  {iid:40} {outcome:10} ${cost:6.2f} {results[iid]['duration_s']:5}s "
                          f"patch {len(patch):6}B", flush=True)
                    if on_result:
                        on_result(iid, results[iid])

            print(f"\nrunning {len(instances)} task(s), {args.parallel} at a time, ${args.budget} each, "
                  f"ceiling ${args.ceiling}", flush=True)
            await asyncio.gather(*(one(i) for i in instances))
        settings = rs.all_values()
    finally:
        await ev_reviewer.stop(rev)
        keep_reviewer_files(root / "reviewer", run_dir / "reviewer")
    return {"results": {**results, **skipped}, "runtime_settings": settings}


def _grade_into(run_dir: Path, run_id: str, workers: int) -> None:
    """Grade a run's predictions with the official harness and fold the
    result into its summary -- which exists before grading, so a harness that
    fails loses nothing."""
    summary = json.loads((run_dir / "summary.json").read_text())
    ids = [json.loads(ln)["instance_id"] for ln in (run_dir / "predictions.jsonl").read_text().splitlines() if ln]
    official = sb.grade(run_dir / "predictions.jsonl", ids, run_id, run_dir, max_workers=workers)
    resolved = set(official.get("resolved_ids") or [])
    summary.update({"graded": True, "resolved": len(resolved),
                    "resolved_rate": round(100 * len(resolved) / len(ids), 1) if ids else None,
                    "official_report": f"{sb.MODEL_NAME}.{run_id}.json",
                    "harness": {k: official.get(k) for k in official if k.endswith("_instances")}})
    notes = official.get("failure_reasons") or {}
    for i in ids:
        summary["instances"].setdefault(i, {})["resolved"] = i in resolved
        if i in notes:
            summary["instances"][i]["harness_note"] = notes[i]
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print(f"\n{len(resolved)}/{len(ids)} resolved by the official harness ({summary['resolved_rate']}%)"
          f"  ${summary.get('total_cost_usd', 0):.2f}\n  written to {run_dir}")


def _gold_check(args, ids: list[str]) -> int:
    """The environment's own test: the reference fix, graded exactly as a
    prediction is. A task whose reference fix fails cannot be resolved by
    anyone in these images -- found 2026-09-24 on django__django-10097, whose
    2018 Django breaks on the image's SQLite 3.45 (evals/SWEBENCH.md)."""
    run_id = args.run_id or time.strftime("gold-%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    report = sb.grade(Path("gold"), ids, run_id, run_dir, max_workers=args.grade_workers, predictions_arg="gold")
    failing = sorted(set(ids) - set(report.get("resolved_ids") or []))
    (run_dir / "gold-check.json").write_text(json.dumps(
        {"run_id": run_id, "dataset": sb.DATASET, "checked": len(ids), "checked_ids": sorted(ids),
         "reference_fails": failing,
         "harness": {k: report.get(k) for k in report if k.endswith("_instances")}}, indent=1))
    print(f"\nreference fix resolves {len(ids) - len(failing)}/{len(ids)}; fails on: {', '.join(failing) or 'none'}"
          f"\n  written to {run_dir}")
    return 0


def main(argv=None) -> int:
    args = _args(argv)
    # A stopped run (kill, pm2, a shutdown) unwinds like Ctrl-C, so its
    # reviewer pair and temporary repositories go with it instead of
    # outliving it.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    if args.grade_only:
        _grade_into(RUNS / args.grade_only, args.grade_only, args.grade_workers)
        return 0
    instances = sb.select(sb.load_instances(), ids=args.instances,
                          sample=args.sample, seed=args.seed)
    instances = shard(instances, args.shard)
    if args.gold_check:
        return _gold_check(args, [i["instance_id"] for i in instances])
    if args.coder_reasoning == "off":
        os.environ["CODER_REASONING"] = "off"
    run_id = args.run_id or time.strftime("tektonix-%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    started = time.time()
    # This run's tool events beside it, not in production's log (tool_events.py).
    tool_events.redirect(run_dir / "tool_events.jsonl")
    # The box and the router, once a minute, beside the run (host_metrics.py).
    sampler = host_metrics.Sampler(run_dir, started).start()
    ids = [i["instance_id"] for i in instances]
    results: dict = {}
    resolved: set = set()
    graded_ids_so_far: set = set()
    # The harness's own reading of an unresolved task (its `failure_reasons`):
    # `no_tests_collected` on pytest's own suite is the inner sessions
    # printing "collected 0 items", not our failure (2026-09-25).
    harness_notes: dict = {}
    settings: dict = {}
    size = max(1, args.batch_size)
    batches = [instances[k:k + size] for k in range(0, len(instances), size)]

    def write_summary(graded: bool, stopped: str | None = None, state: str = "running") -> dict:
        """After every task, so the dashboard (agent/routers/swebench.py)
        shows a run as it goes. `state` is running, done or stopped; `pid`
        lets it tell a running run from one that died without saying so."""
        summary = {
            "run_id": run_id, "dataset": sb.DATASET, "notes": args.notes,
            "state": state, "pid": os.getpid(),
            "diagnostic": run_id.startswith("diag-"),
            "selection": {"instances": args.instances, "sample": args.sample, "seed": args.seed, "all": args.all,
                          "shard": args.shard},
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "parallel": args.parallel, "budget_usd": args.budget, "batch_size": size,
            "task_timeout_min": args.task_timeout_min,
            "total": len(ids), "graded": graded, "stopped_early": stopped,
            "resolved": len(resolved) if graded else None,
            "resolved_so_far": len(resolved), "graded_so_far": len(graded_ids_so_far),
            "resolved_rate": round(100 * len(resolved) / len(ids), 1) if graded and ids else None,
            "total_cost_usd": round(sum(r.get("cost_usd", 0) for r in results.values()), 4),
            "runtime_settings": settings,
            "coder_reasoning": args.coder_reasoning,
            "host": sampler.record(),
            "instances": {i: {**results.get(i, {"outcome": "not_run"}),
                              **({"resolved": i in resolved} if graded or i in graded_ids_so_far else {}),
                              **({"harness_note": harness_notes[i]} if i in harness_notes else {})}
                          for i in ids},
        }
        tmp = run_dir / "summary.json.tmp"
        tmp.write_text(json.dumps(summary, indent=1))
        tmp.replace(run_dir / "summary.json")      # never read half-written
        return summary

    def task_done(iid: str, row: dict) -> None:
        results[iid] = row
        write_summary(graded=False)

    write_summary(graded=False)
    try:
        return _batches(args, run_id, run_dir, ids, batches, results, resolved, graded_ids_so_far,
                        harness_notes, settings, write_summary, task_done)
    except BaseException as e:
        # Ctrl-C, a kill, or a crash: the summary says which, so the page does
        # not show a dead run as running.
        why = "stopped by the operator" if isinstance(e, KeyboardInterrupt) else f"crashed: {type(e).__name__}: {e}"
        sampler.stop()
        write_summary(graded=False, stopped=why[:500], state="stopped")
        raise
    finally:
        sampler.stop()


def _batches(args, run_id, run_dir, ids, batches, results, resolved, graded_ids_so_far,
             harness_notes, settings, write_summary, task_done) -> int:
    """Each batch run, graded, and its images deleted. The collections are
    main()'s, updated in place, so its summary always sees the latest."""

    stopped = None
    for n, batch in enumerate(batches, 1):
        pct = disk_used_pct()
        if pct >= args.max_disk_pct:
            stopped = f"disk at {pct:.0f}% before batch {n} (limit {args.max_disk_pct:.0f}%)"
            print(f"\nSTOPPING: {stopped}", flush=True)
            break
        print(f"\n=== batch {n}/{len(batches)}: {len(batch)} task(s), disk {pct:.0f}% ===", flush=True)
        root = Path(tempfile.mkdtemp(prefix="tektonix-swebench-"))
        try:
            out = asyncio.run(_run(args, batch, root, run_dir,
                                   spent_before=sum(r.get("cost_usd", 0) for r in results.values()),
                                   on_result=task_done))
        finally:
            if not args.keep:
                shutil.rmtree(root, ignore_errors=True)
        results.update(out["results"])
        settings.update(out["runtime_settings"] or {})
        write_summary(graded=False)
        ran = [i["instance_id"] for i in batch
               if results.get(i["instance_id"], {}).get("outcome") not in ("not_run", "setup_error")]
        if ran and not args.skip_grade:
            # Graded now, while this batch's images are still here.
            report = sb.grade(run_dir / "predictions.jsonl", ran, run_id, run_dir, max_workers=args.grade_workers)
            resolved |= set(report.get("resolved_ids") or [])
            harness_notes.update(report.get("failure_reasons") or {})
            graded_ids_so_far.update(ran)
            write_summary(graded=False)
            print(f"  batch {n}: {len(set(ran) & resolved)}/{len(ran)} resolved; "
                  f"{len(resolved)} so far", flush=True)
        if not args.keep_images:
            for inst in batch:
                subprocess.run(["docker", "image", "rm", "-f", inst.get("image") or sb.image_name(inst["instance_id"])],
                               capture_output=True)
            subprocess.run(["docker", "image", "prune", "-f"], capture_output=True)
            print(f"  batch {n}: images deleted, disk now {disk_used_pct():.0f}%", flush=True)

    if args.skip_grade:
        write_summary(graded=False, stopped=stopped, state="stopped" if stopped else "done")
        print(f"\nnot graded; predictions in {run_dir}. Grade with --grade-only {run_id}")
        return 0
    graded_ids = [i for i in ids if results.get(i, {}).get("outcome") not in (None, "not_run", "setup_error")]
    if graded_ids:
        # One official report for the whole run, from the harness's own logs
        # of every batch -- nothing is re-run.
        final = sb.grade(run_dir / "predictions.jsonl", graded_ids, run_id, run_dir,
                         max_workers=args.grade_workers, rewrite=True)
        resolved.clear()
        resolved.update(final.get("resolved_ids") or [])
        harness_notes.update(final.get("failure_reasons") or {})
    summary = write_summary(graded=True, stopped=stopped, state="stopped" if stopped else "done")
    print(f"\n{len(resolved)}/{len(ids)} resolved by the official harness ({summary['resolved_rate']}%)"
          f"  ${summary['total_cost_usd']:.2f}" + (f"\n  STOPPED EARLY: {stopped}" if stopped else "")
          + f"\n  written to {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
