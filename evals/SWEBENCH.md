# SWE-bench Verified

Persei, run on SWE-bench Verified and graded by the official harness.
`scripts/run_swebench.py` does both; `agent/evals/swebench.py` is the
module.

## What makes it a SWE-bench number

These are the conditions under which "X% on SWE-bench Verified" is a true
statement. Changing any of them makes it a different benchmark.

| | |
| --- | --- |
| tasks | `SWE-bench/SWE-bench_Verified`, the published 500, unmodified (the dataset's current official home; the older `princeton-nlp` copy has the same tasks but lacks the `image` field the harness now needs) |
| input | the issue (`problem_statement`) and the repository at the task's commit. Never `hints_text`, `patch`, `test_patch`, `FAIL_TO_PASS` or `PASS_TO_PASS` |
| environment | each task's official image, `swebench/sweb.eval.x86_64.<id>`, with the code at `/testbed` and its `testbed` conda env active |
| attempts | one per task (pass@1) |
| isolation | each task is its own project: no memory, episode or history is shared between tasks |
| internet | none. The published fix is on GitHub, so a benchmark project has no GitHub, web-page or cross-project tools, and its sandbox runs with `--network none` |
| no answer-hunting | the task statement says the fix and the grading tests are not in the environment, and a benchmark project's shell refuses searches for them with the same explanation (`agent/tools/benchmark_guard.py`): git's object store and unreachable objects, other branches, tags and refs, `gh`, `pip download`/`install`, `curl`/`wget`, filesystem-wide `find`/`grep`, caches outside the repository, and reading or decompiling stale `.pyc` files. A compound command loses only its hunting segments; the rest runs, with the refusal named on its result. The first samples spent 15% of their shell commands on this, one curling the fix's own pull request. Only the sandbox's missing network stopped it |
| grading | `swebench.harness.run_evaluation`, the official harness, in its own environment. Persei never grades itself |

The repository is copied out of the task's image, not cloned. Several
(astropy, scikit-learn) are built in place there, and the image's git history
already ends at the task. The runner checks that no commit is reachable
beyond it before a task starts, and refuses the task if one is.

Every word the harness itself puts into the agent's conversation -- the task framing, a guard's refusal, a checkpoint, the ship gate's feedback, a handover to the fallback seat -- starts with `[Persei harness]` (`agent/harness_voice.py`), so a trajectory shows plainly which text was the system's and which was the model's.

## What is Persei's own, and so part of what is measured

- **The agent:** the same graph, models, prompts and tools as production,
  minus the tools listed above. The runtime settings are production's, read
  from the live store and recorded in the summary.
- **Its gate:** a benchmark project declares its own checks. Every changed
  Python file must compile, and the package must import. They are light on
  purpose: the grading tests are hidden, and a full suite (Django's) runs for
  hours.
- **The independent reviewer:** it reviews every diff, as it does in
  production. It runs no checks of its own here.
- **The prediction:** the task's committed and uncommitted changes against
  the image's commit. Test files are left out: the harness applies the task's
  own test patch after the prediction, and a clash fails the task. Build
  output and caches are left out too.
- **The baseline:** `/baseline` is the untouched tree, read-only. For an
  editable install (Django's `runtests.py`, most of these repositories) a test
  run started there still imports the package from `/workspace`, so the shell
  sets `PYTHONPATH=/baseline` for any command that goes there. Before that
  (2026-09-25) "fails on /baseline too" was a comparison of the patched code
  with itself.

## What the 2026-09-25 sample changed

40 of 50 (seed 1), $18.73 billed. Read against the trajectories, the ten
failures were five harness problems, each fixed the same day; the fix itself
was the cause in none of them:

| what | how it showed | change |
| --- | --- | --- |
| empty, length-capped replies | 65 calls spent 32,768 output tokens on reasoning and returned nothing: $2.32, 2.7 h, six of the ten failures. The coder ignores every reasoning cap the router can send | retried once on the fallback seat at low effort inside the same turn (`agent/middleware/empty_reply.py`); the coordinator's output cap is 16k |
| a verifier cut off without a report | 13 of 48 runs ended at the tool-call cap with "Tool call limit reached" and nothing else | on its last allowed call a bounded subagent has no tools and must report; countdown and counters are per invocation; the test-writer is bounded the same way; the verifier bills as `agent-verifier` |
| `/baseline` tested the workspace | "pre-existing on baseline" said 39 times; one task that passed twice before failed | `PYTHONPATH=/baseline` set by the shell, and the prompts say why |
| a loop with a changing address in its output | one command 352 times, $1.15 | results normalised before hashing; eight identical calls are refused regardless |
| the guard refused whole compound commands | the legitimate half of `git log --all ... && grep ...` was lost, twice on the path to the failing case | only the hunting segments are refused, and named; `.pyc` reading, `git tag`, `git branch -a`, `gh` are hunts too |

The model wrote "matches the upstream fix" in 23 of 50 tasks. The guard stops
it searching the disk; nothing stops it remembering. The task statement now
says a verified change is never rewritten to match a remembered one, which is
what three of the failures did.

## Setup (once)

    python3 -m venv /opt/swebench/venv
    /opt/swebench/venv/bin/pip install swebench datasets
    /opt/swebench/venv/bin/python -c "import datasets, json; \
      ds = datasets.load_dataset('SWE-bench/SWE-bench_Verified', split='test'); \
      json.dump([dict(r) for r in ds], open('/opt/swebench/verified.json', 'w'))"

The harness lives in its own environment so its dependencies never meet
Persei's. `SWEBENCH_PYTHON` and `SWEBENCH_DATASET_JSON` override the paths.

## Running

From the dashboard: the **Benchmarks** page (admin) starts a run -- sample size
(50, 100, 250 or all 500), seed, tasks at once and the per-task budget -- as
the same runner below, in its own session so a restart of the app does not
end it, split into processes of five tasks (`--shard`). The page stops a run
too (the runner's orderly stop; nothing is graded until `--grade-only`), and
shows the runner's log under the run.

From a shell:

    scripts/run_swebench.py --instances psf__requests-1142 django__django-10097
    scripts/run_swebench.py --sample 50 --seed 1 --parallel 3
    scripts/run_swebench.py --all --parallel 4

`--budget` caps each task ($3 by default) and `--ceiling` the whole run.
`--coder-reasoning off` runs the coder seat with its chain of thought disabled,
for this process only; the summary records it.
SWE-bench sets no time limit. `--task-timeout-min` (180) only guards against
a hung task. The budget is what bounds the work, as it does in production.

The tasks run in batches of `--batch-size` (20). A batch's images are pulled,
its tasks run, the harness grades them while the images are still there, and
then the images and the batch's repositories are deleted. All 500 images
together are over a terabyte. No image is pulled, and no batch starts, while
the disk is at `--max-disk-pct` (80%) or fuller. The run stops there, and
the tasks it never reached are recorded as `not_run` with the reason.
`--keep-images` keeps the images. At the end, the harness rebuilds one report
for the whole run from its own logs of every batch (`--rewrite_reports`),
without running anything again.

`--shard K/N` runs every N-th task of the selection, starting at the K-th, so
several runner processes share one sample (each with its own `--run-id`,
SQLite file and reviewers): `--shard 1/2` and `--shard 2/2` side by side
run six tasks at once as two processes of three. A run never traces to
LangSmith. Each task sees its untouched tree read-only at `/baseline`, to run
the code as it was before its change (the sandbox's `.git` is read-only).

The **SWE-bench Verified** section of the Analytics page shows every run as it
goes, with each task's result, the tests the harness ran, the patch and the
agent's whole conversation. A run whose id starts with `diag-` is labelled
diagnostic and is never shown as a score.

Everything goes to `logs/swebench/<run-id>/`:
- `predictions.jsonl`: the submission, in the harness's format.
- `tektonix.<run-id>.json`: the official harness's report (`resolved_ids`
  and the rest).
- `grading.log`: the harness's output.
- `summary.json`: per task, the outcome, cost, time, patch size, the
  reviewer's verdict and its whole record (`review`: summary, findings, the
  message it sent the agent), the harness's own note on an unresolved task
  (`harness_note`, e.g. `no_tests_collected`) and **which models the router
  served**; for the run, the runtime settings, `coder_reasoning` and the host
  peaks (`host`).
- `host.jsonl`: the box and the router once a minute while the run went --
  memory, CPU, load, disk, running containers, OOM kills, and the router's
  calls, in-flight peak, median and p90 latency and errors. The peaks are in
  `summary.json` (`host`), and the Analytics page shows the series under the
  run, so "can we run more at once" is answered from the run itself.
- `reviewer/`: the reviewer pair's history.jsonl, reviewer.log and state.json
  for the run, copied out of each batch's temporary root before it is
  deleted.

## Validating the environment: the reference fixes

    scripts/run_swebench.py --gold-check --all

This grades the dataset's own reference fix for each task, exactly as a
prediction is graded. A task whose reference fix fails cannot be resolved by
anyone in these images. Run it before a full run, and publish the list with
the score.

**Known: `django__django-10097`.** Its code is Django from 25 June 2018, which
migrates SQLite tables by renaming them through `<table>__old`. SQLite 3.26 and
later break that (Django ticket #29182, fixed in 2.1.5/2.2), and the official
image ships SQLite 3.45.3. Its test patch changes only data files, so the
harness runs Django's entire suite. Migrations then corrupt the test database
partway through, with `no such table: main.django_site__old` and about 985
errors after it, 7 of them in the task's FAIL_TO_PASS. The reference fix fails
exactly as our agent's identical fix does (checked 2026-09-24). It is the only
Django task with both conditions: the other three built on pre-fix code run
only their own test modules. Fixing it means changing the image, which is
SWE-bench's to do. Doing it here would make the result not SWE-bench.

## Advertising a result

- **Only a full run is "SWE-bench Verified".** Quote it with the date, the
  harness version (`pip show swebench`), the models (`summary.json`), and the
  gold-check's list of tasks no one can resolve in the official images. A
  sample is "N of a 50-task sample", never "N%" alone.
- **The leaderboard** (swebench.com) wants the predictions, the harness's
  logs and the agent's trajectories. The runner writes each task's full
  conversation, every message and tool call, to `trajectories/<id>.json` in the
  run folder.
