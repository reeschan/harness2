# Benchmark guides

Complete the [installation and credentials](SETUP.md) first. Each
benchmark installs separately; install the ones you intend to run. Every command
below takes `--substrate cx` to use the
[Codex executor](SETUP.md#codex-executor) instead of OpenCode. Choose the
recursion mode with `--mode parallel` or `--mode sequential` and the task-level
recursion budget with `--k`. Keep both identical across the run, judge, and
report commands.

## LAB

Upstream: [harveyai/harvey-labs](https://github.com/harveyai/harvey-labs).
Legal work tasks with source documents and rubric-based grading. The adapter
groups tasks by practice area and removes criteria from the material passed to
the improver. Needs Pandoc and Poppler.

```bash
harness2-setup lab
harness2-check --bench lab
harness2 --bench lab --k 1 --mode parallel
harness2-judge --bench lab --k 1 --mode parallel
harness2-report --bench lab --k 1 --mode parallel
```

Sequential recursion runs the same commands with `--mode sequential`.

Tasks arrive with the pinned checkout at
`dependencies/harvey-labs/tasks/<practice-area>/<task>/task.json`, and the adapter
discovers all installed tasks; no separate task list is required. Setup installs
the LAB dependency environment, the runner overlays, and the included Vertex
judge patch.

The judge reads outputs only after selection. Reports use Partial (fraction of
criteria passed) and All-pass (all criteria passed). A failed judge remains
unscored and writes its output to `judge.log` in the run directory; a Vertex
`429 RESOURCE_EXHAUSTED` there means quota, so rerun `harness2-judge` later.
Changing the judge model changes the measurement. Outputs are under
`runs/lab/`.

## JobBench

Upstream: [Job-Bench/job-bench-eval](https://github.com/Job-Bench/job-bench-eval).
Data: [JobBench/job-bench](https://huggingface.co/datasets/JobBench/job-bench).
Professional tasks, with the library grouped by the dataset's occupation
directories.

```bash
harness2-setup jb
harness2-check --bench jb
harness2 --bench jb --k 1 --mode parallel
harness2-judge --bench jb --k 1 --mode parallel
harness2-report --bench jb --k 1 --mode parallel
```

Sequential recursion runs the same commands with `--mode sequential`.

Setup downloads only the main dataset from Hugging Face — public and ungated, so
no account, token, or terms acceptance is required — records its revision, and
places tasks at `dependencies/job-bench-eval/dataset/main/`. It also installs the
document extraction dependencies and a judge patch that handles reasoning effort
and empty completions. Rubrics stay outside the improver's evidence.

With a Gemini `HARNESS2_JUDGE_MODEL`, the judge uses the project's Vertex
OpenAI-compatible endpoint and Application Default Credentials. For a separate
judge provider, set `HARNESS2_JUDGE_BASE_URL`, `HARNESS2_JUDGE_API_KEY`, and the
model ID that endpoint accepts. The reported score uses the upstream rubric
weights; unscored tasks are reported separately. Results are under `runs/jb/`.

Note: JobBench data, including `RUBRICS.json`, is public on Hugging Face, so an
executor with web access may retrieve it. Block those hosts during runs as
described under [Web access](EXPERIMENTS.md#web-access).

## WorkBuddy-Bench

Upstream: [Tencent/workbuddy-bench](https://github.com/Tencent/workbuddy-bench).
Data: [tencent/workbuddy-bench](https://huggingface.co/datasets/tencent/workbuddy-bench).
Office, web, and code subsets. Needs Docker.

The upstream custom Tencent license includes a territorial limitation; read
[the license](https://github.com/Tencent/workbuddy-bench/blob/main/LICENSE)
before downloading or using the benchmark. Derived runner files and patches
retain that license.

```bash
docker info
harness2-setup wb --subset office
harness2-check --bench wb --subset office
harness2 --bench wb --subset office --k 1 --mode parallel
harness2-judge --bench wb --subset office --k 1 --mode parallel
harness2-report --bench wb --subset office --k 1 --mode parallel
```

Sequential recursion runs the same commands with `--mode sequential`.

`harness2-setup wb` without `--subset` installs all three subsets in one run;
pass `--subset` to install only the one you intend to run. Every other command
takes one subset at a time.

Setup installs the pinned Harbor-based runtime, applies the included adapter,
mount, and credential/transport patches, copies the harness runners, creates
solver and judge routes from `.env`, downloads the requested subset with the
upstream fetch script, and builds the OpenCode harness image. The public Vertex
setup uses a `google-vertex/gemini...` executor and a bare `gemini...` judge ID;
no model YAML or account-specific proxy configuration needs to be copied from
another checkout. Setup refuses to run unless `HARNESS2_SOLVER_MODEL` is a
`google-vertex/gemini...` ID and `HARNESS2_JUDGE_MODEL` a bare `gemini...` one,
with `GOOGLE_CLOUD_PROJECT` set; `--dry-run` reports the requirement and still
prints the rest of the plan. The benchmark's job-local proxy refreshes ADC tokens
and preserves Gemini tool-call thought signatures.

Setup patches the upstream fetch script to download with `curl` (then `wget`,
then `hf`) and to require the dataset repository's `SHA256SUMS`: an archive that
cannot be verified is not unpacked. A verified download prints
`wb-bench-<subset>-v1.0.tar.gz: OK`.

Tasks are discovered from `task.toml` and `instruction.md` under
`dependencies/workbuddy-bench/datasets/wb-bench-<subset>-v1.0/tasks/`. Libraries use
`metadata.role` on code and `metadata.category` on office and web. Each rollout
uses a fresh benchmark container. The adapter reads the verifier's `score.json`;
the improver never receives verifier output. Missing verifier results remain
unscored. Runs live under `runs/wb/<subset>/`.

For Codex, start the bridge with `harness2-bridge --host 0.0.0.0` so that the containers can reach it.

## Task intake (non-benchmark tasks)

Task intake runs Harness² on tasks that do not come from a benchmark. A task
connector polls for raw tasks and fetches their attachments; a normalizer turns
each raw task into a `TaskSpec`; `IntakeAdapter` stores the spec and exposes the
tasks through the same adapter interface as the benchmarks, so the existing loop
(`schedule.run_task`) runs them unchanged. `run_intake` then hands the chosen
run's `workspace/output/` back to the connector, or reports the task as failed.

### The local folder connector

The default connector, `local` (`harness2/core/intake_local.py`), needs no
outside service. Everything lives under one root folder:

| Path | Contents |
|---|---|
| `inbox/<id>/task.json` | Optional. `{"title": ..., "body": ..., "meta": {...}}`; a `task_spec` entry in `meta` is used as is by the `passthrough` normalizer |
| `inbox/<id>/task.md` | Optional. The body; the title is the first `# ` heading, or the id. When both files exist, `task.json` wins |
| `inbox/<id>/files/` | Optional. The attachments |
| `outbox/<id>/` | A delivered task: a copy of the deliverable directory plus `summary.json` |
| `failed/<id>.json` | A failed task: `{"task_id": ..., "reason": ...}` |

A task id must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`; other inbox folders
are skipped. A task with an outbox folder or a failed record is not polled
again. Symbolic links are never followed or copied.

### Choosing a connector and a normalizer

| Variable | Default | Values |
|---|---|---|
| `HARNESS2_TASK_CONNECTOR` | `local` | A registered name, or a `module:Attr` import path to a connector factory |
| `HARNESS2_NORMALIZER` | `passthrough` | `passthrough`, `model`, or a `module:Attr` import path to a normalizer factory |

`passthrough` reads an already structured task, from `meta["task_spec"]` or
from the body as JSON, without a model. `model` asks a model to write the spec;
use it only when the `normalizer` route has a model configured
(`config.ROLES["normalizer"]`) or when you pass `proposer=` to
`normalizer_for`, otherwise it stops with an error. Either way, a spec that
fails validation is retried once with the problems as feedback, and a second
failure sends the task to `connector.fail`.

### TaskSpec keys

The normalizer's output is checked against `TASK_SPEC_SCHEMA` in
`harness2/core/intake.py`. Unknown keys are rejected.

| Key | Required | Type | Notes |
|---|---|---|---|
| `task_id` | yes | string | Pattern `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`; must equal the incoming id |
| `domain` | yes | string | Same pattern; reuse an existing domain when one fits |
| `prompt` | yes | string | The task as the worker reads it; at least 1 character |
| `files` | yes | array of string | Attachment paths relative to the task's files directory; empty means every attachment |
| `deliverable` | yes | object | `what` (required string) and `where` (optional string, a path relative to `output/`) |
| `prompts` | yes | object | `work` (required), `domain_word`, `deliverable_word`, `yardstick` (optional strings) |
| `private_criteria` | no | array of string | Acceptance criteria hidden from the agent (see below) |

Paths in `files` and `deliverable.where` must be relative, use `/`, and contain
no `..` segment.

### Private criteria

`private_criteria` are stored only in `<root>/private/<task_id>.json`. They
never enter the task's `spec.json`, its files, the rollout workspace, the
prompts, or the evidence and context that the improver reads, and an attachment
named `private_criteria*` is not ingested. The improver never sees them; only
the judge passed to `IntakeAdapter` reads them, through `score()`, which runs
after selection. Without a judge, `score()` returns `None` and the method still
runs: selection never looks at scores.

### Running from Python

```python
from pathlib import Path

from harness2.benchmarks.intake import IntakeAdapter, run_intake
from harness2.core.intake import connector_for
from harness2.core.normalize import normalizer_for

adapter = IntakeAdapter(
    Path("runs/intake"),
    connector_for(root="intake-root"),  # the local folder connector
    normalizer_for(),                   # passthrough by default
    launcher=my_launcher,               # your Launcher implementation
    channel=prop.spec,
)
results = run_intake(adapter, prop, k=2, runs_root=Path("runs/intake/runs"))
```

`my_launcher` is your own object with a `launch(*, workspace, prompt, install,
run_dir, timeout)` method: it runs the agent in `workspace`, leaves the
deliverables in `workspace/output/`, and writes `run_dir/trajectory.md`. `prop`
is the improver's `Proposer`. A launcher that starts rollouts on a real
substrate will be added in a later release.
