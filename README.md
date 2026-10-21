<h3 align="center">
  <img src="assets/Harness2-wordmark.png" alt="Harness²" width="360"><br>
  Recursive Agent Harnessing for an Open World
</h3>

<p align="center">
  <a href="https://arxiv.org/pdf/2610.14224"><img src="https://img.shields.io/badge/Paper-arXiv-EA4335?logo=arxiv&logoColor=white" alt="Paper"></a>
  <a href="https://harness2.github.io"><img src="https://img.shields.io/badge/Website-harness2.github.io-4285F4?logo=googlechrome&logoColor=white" alt="Website"></a>
  <a href="docs/BENCHMARKS.md"><img src="https://img.shields.io/badge/Docs-Benchmark%20guides-FBBC04" alt="Benchmark guides"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-34A853" alt="License"></a>
</p>

<p align="center">
  <b>English</b> · <a href="README.ja.md">日本語</a>
</p>

<p align="center">
  <a href="docs/BENCHMARKS.md#jobbench">
    <img src="assets/harness2-setup.gif" alt="Install, set up, and run JobBench: uv pip install -e '.[render]', harness2-setup jb, then harness2 --bench jb --k 1 --mode parallel. Click for prerequisites and commands." width="640">
  </a>
</p>

## In one paragraph

A **harness** is everything around a model that turns it into an agent: prompts,
tools, memory, skills, orchestration, and more. **Harnessing** is optimizing
that system. Harness² does this automatically, per task, **without changing
model weights and without ground-truth feedback**: it proposes edits to the
harness, runs them on the same task, composes a better harness from the
behavioral differences it observes, and remembers what worked for the next task
in the same domain.

## Contents

- [Key ideas](#key-ideas)
- [Architecture](#architecture)
- [Harness components](#harness-components)
- [Install](#install)
- [Run a benchmark](#run-a-benchmark)
- [Experiments](#experiments)
- [Web access](#web-access)
- [Results and resume](#results-and-resume)
- [Repository layout](#repository-layout)
- [Video overview](#video-overview)
- [Citation](#citation) · [Contributing](#contributing) · [License](#license) · [Disclaimer](#disclaimer)

## Key ideas

Harness² targets an **open world**, where agents meet tasks that were never
seen while the harness was designed. It connects two levels of recursion:

| Level | What it improves | How |
|---|---|---|
| **Task-level recursion** | The *execution harness* for the current task | A contrastive **Propose–Probe–Compose** step: propose edits, probe their effect on the same task, and compose a refined harness from the contrasts between executions |
| **Harnessing-level recursion** | The persistent *improvement harness* | After each task, store the adopted harness and evidence-supported lessons in a per-domain library that guides future refinement |

<p align="center">
  <a href="assets/harness2_pipeline.png">
    <img src="assets/harness2_pipeline.png" alt="Harness² pipeline. Task-level recursion: an agent executor runs the initial harness, a harness editor proposes invariant and domain edits, candidate probes execute them, and a harness composer combines them into the final harness. Harnessing-level recursion: an experience reflector reflects on the step and updates the improvement harness used for the next task." width="100%">
  </a>
</p>

**Results.** Across three professional benchmarks and four model–harness
configurations, a single Harness² step improves GLM-5 and Gemini 3.5 Flash over
their base harnesses: up to **+11.5 points** on WorkBuddy-Bench, **+4.5 points**
on JobBench, and **+11.8 percentage points** on LAB's partial pass rate. A larger
task-level recursion budget brings further gains.

**This release supports** parallel and sequential recursion, OpenCode and Codex
executors, and the JobBench, LAB, and WorkBuddy-Bench benchmarks.

## Architecture

The code is split along three independent axes: the **method** (`harness2/core`,
benchmark- and executor-independent), the **substrate** that executes a rollout
(OpenCode or Codex), and the **benchmark** adapter that supplies tasks,
launchers, and grading.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/architecture/architecture-dark.svg">
    <img src="assets/architecture/architecture-light.svg" alt="Harness² architecture. The CLI (harness2-setup/-check, harness2, harness2-demo, harness2-judge/-report) drives harness2.core. In core, schedule runs k shared base rollouts per task; task-level recursion runs Propose, Probe and Compose using the c1..c8 harness tree and an answer-free evidence tree; selection blindly picks one of the k finals and reflects on it; the adopted harness and lesson go into the per-domain library, a MemoryConnector (FileConnector by default, swappable for another connector), which guides the next task. Core calls out to execution and evaluation: benchmark adapters (JobBench, LAB, WorkBuddy-Bench) launch rollouts on a substrate (OpenCode or Codex CLI), which calls Vertex AI models (Gemini executor and judge; Claude or Gemini improver). Outputs are written to runs/." width="100%">
  </picture>
</p>

What happens for one task:

1. **Base rollouts.** `schedule` runs the task `k` times with the current base
   harness. These executions are shared by both recursion modes.
2. **Propose.** Improver sessions read an evidence tree (`context`: the task,
   full trajectories, deliverables, and the harness, never grader verdicts) and
   emit edits to the eight harness components (`tree`). Edits are either
   *invariant* (general) or *domain-specific*.
3. **Probe.** Each candidate harness is executed on the same task through the
   benchmark adapter and the chosen executor.
4. **Compose.** A composer merges what the behavioral contrasts support into a
   refined harness. Sequential mode repeats Propose–Probe–Compose `k` times.
5. **Select and reflect.** `selection` blindly picks one of the `k` finals
   without answers or scores, then reflects on why it worked.
6. **Remember.** `library` stores the adopted harness and the lesson for the
   domain. Later tasks in that domain read it, which is harnessing-level
   recursion.

Judging (`harness2-judge`) runs only afterwards and scores only the selected
run; nothing in the method reads a score. Budgets are fixed per mode at
`R = 4k` rollouts and `P = 3k` improvement stages (`core/ledger.py`), and every
finished rollout is fingerprinted so reruns resume safely (`core/runmark.py`).

The diagram source is [`assets/architecture/src`](assets/architecture/src);
regenerate it with `python3 assets/architecture/src/render.py` (requires
[D2](https://d2lang.com)).

## Harness components

Harness² exposes eight editable components around the model: **system prompt,
guardrails, memory, tool guidance, subagents, skills, scripts, and plugins**
(`c1`..`c8` in the code). They control context construction, the tool
interface, delegation, and behavior inside the tool loop.

<p align="center">
  <a href="assets/components.png">
    <img src="assets/components.png" alt="Eight harness components grouped by runtime role: system prompt, guardrails, memory, and skills construct context; tool guidance and scripts define the tool interface; subagents orchestrate delegated work; plugins control middleware." width="100%">
  </a>
</p>

## Install

Use **Linux, Python 3.12, and [uv](https://docs.astral.sh/uv/getting-started/installation/)**.
From the repository root:

```bash
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e '.[render]'
harness2-demo --output runs/demo --k 2
```

The local demo runs both recursion modes with deterministic editors and real
Python executions. **It requires no model credentials**, so it is the quickest
way to see the workflow end to end.

For model-backed benchmarks, install **Node.js 20+, the Google Cloud CLI**, and
the [benchmark dependencies](docs/SETUP.md#requirements). LAB needs Pandoc and
Poppler; WorkBuddy-Bench needs Docker. Then configure your models:

```bash
cp .env.example .env
# Edit .env with your Vertex AI project and model IDs.
gcloud auth application-default login
```

See the [setup guide](docs/SETUP.md) for model settings, authentication
alternatives, and the [Codex executor](docs/SETUP.md#codex-executor).

## Run a benchmark

Every benchmark follows the same five steps:

| Step | Command | What it does |
|---|---|---|
| 1. Install | `harness2-setup <bench>` | Installs pinned tools, runner overlays, dependencies, and data |
| 2. Check | `harness2-check --bench <bench>` | Verifies the benchmark is ready |
| 3. Refine | `harness2 --bench <bench> ...` | Runs task-level and harnessing-level recursion |
| 4. Judge | `harness2-judge --bench <bench> ...` | Scores the selected run of each task |
| 5. Report | `harness2-report --bench <bench> ...` | Prints the benchmark's headline figure |

For example, to install LAB and run, judge, and report one task:

```bash
harness2-setup lab
harness2-check --bench lab
harness2 --bench lab --k 1 --mode parallel \
  --only-domains antitrust-competition --max-tasks 1
harness2-judge --bench lab --k 1 --mode parallel
harness2-report --bench lab --k 1 --mode parallel
```

> [!IMPORTANT]
> Model-backed runs incur API costs; start with a small task selection. Add
> `--dry-run` to `harness2` to preview task selection without model calls.
> Judging and reporting must use the same benchmark, executor, subset, `--k`,
> `--mode`, and seed as the refinement run.

| Benchmark | Tasks | Install | Guide |
|---|---|---|---|
| JobBench | Professional work | `harness2-setup jb` | [JobBench](docs/BENCHMARKS.md#jobbench) |
| LAB | Legal work | `harness2-setup lab` | [LAB](docs/BENCHMARKS.md#lab) |
| WorkBuddy-Bench | Office, web, and code | `harness2-setup wb` | [WorkBuddy](docs/BENCHMARKS.md#workbuddy-bench) |

Choose one benchmark per command. WorkBuddy also takes `--subset office`,
`--subset web`, or `--subset code` on every command after setup. The
[benchmark guides](docs/BENCHMARKS.md) cover each one's data, domains, and
grading.

## Experiments

`--k` is the task-level recursion budget. Scale it in two ways:
**parallel recursion** explores multiple candidate harnesses and composes
refinements from their shared execution evidence; **sequential recursion**
carries the refined harness and accumulated evidence through successive
Propose–Probe–Compose steps.

<p align="center">
  <a href="assets/harness2_recursion_modes.png">
    <img src="assets/harness2_recursion_modes.png" alt="Two ways to scale task-level recursion. Parallel: probe k general and k domain-specific candidate harnesses, then produce k compositions from the shared evidence. Sequential: carry the refined harness and accumulated evidence through k Propose–Probe–Compose steps." width="100%">
  </a>
</p>
<p align="center"><em>Parallel recursion explores wider; sequential recursion refines deeper.</em></p>

| Option | Behavior |
|---|---|
| `--k 1` | One Propose–Probe–Compose step |
| `--k K --mode parallel` | Probe `K` general and `K` domain-specific candidates, then produce `K` compositions |
| `--k K --mode sequential` | Refine the harness through `K` successive steps |
| `--substrate cx` | Use Codex instead of the default OpenCode executor |

After each task, harnessing-level recursion retains the adopted harness and
evidence-supported lessons to guide future refinement. See the
[experiment guide](docs/EXPERIMENTS.md) for task selection, concurrency,
reproducibility, and baseline comparisons.

## Web access

> [!WARNING]
> Executor network access is unrestricted, and Codex runs with its sandbox
> disabled. Public benchmark rubrics may be accessible online. Before
> evaluation, configure [network restrictions](docs/EXPERIMENTS.md#web-access)
> to prevent access to benchmark answers and grading criteria.

## Results and resume

Results are saved under `runs/`, organized by benchmark and subset where
applicable. They include task summaries, deliverables, adopted harnesses,
reflection notes, and evaluation results.

Repeat the same command to resume. Use a new `HARNESS2_RUNS_DIR` when changing
models or task selection. See [results and resume](docs/EXPERIMENTS.md#results-and-resume)
for details; each command also supports `--help`.

## Repository layout

| Path | Contents |
|---|---|
| [`harness2/core/`](harness2/core) | The method: scheduling, recursion, prompts, proposer, evidence tree, harness tree, selection, library, budget ledger, judging and reports |
| [`harness2/substrate/`](harness2/substrate) | Executors behind one interface: OpenCode and Codex CLI |
| [`harness2/benchmarks/`](harness2/benchmarks) | Adapters for JobBench, LAB, and WorkBuddy-Bench |
| [`harness2/cli/`](harness2/cli) | The `harness2-*` commands |
| [`harness2/demo.py`](harness2/demo.py) | Deterministic local adapter used by `harness2-demo` |
| [`docs/`](docs) | Setup, benchmark, and experiment guides |
| [`scripts/`](scripts) | End-to-end run scripts and setup/provenance checks |
| [`third_party/`](third_party/README.md) | Runner overlays and patches for upstream benchmarks and executors |
| [`dependencies/`](dependencies/README.md) | Pinned dependency versions and assets |
| [`tests/`](tests) | Workflow tests |

## Video overview

https://github.com/user-attachments/assets/76b10548-b14b-4c8f-9aa5-a93662df4d56

## Citation

```bibtex
@article{xu2026harness2,
  title   = {Harness$^2$: Recursive Agent Harnessing for an Open World},
  author  = {Xu, Ruiyao and Chen, Yanfei and CuiZhu, Zhongying and Dalvi Mishra, Bhavana and Ming, Yifei and Yu, Han and Han, Rujun and Lee, Chen-Yu and Pfister, Tomas},
  journal = {arXiv preprint arXiv:2610.14224},
  year    = {2026},
  url     = {https://arxiv.org/abs/2610.14224}
}
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Harness² is licensed under [Apache 2.0](LICENSE). Third-party code under
[`third_party/`](third_party/README.md), benchmarks, and datasets retain their
upstream licenses; see
[third-party notices](dependencies/README.md).

## Disclaimer

This is not an officially supported Google product. This project is not eligible
for the [Google Open Source Software Vulnerability Rewards Program](https://bughunters.google.com/open-source-security).
