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
| `HARNESS2_TASK_CONNECTOR` | `local` | A bundled name (`local`), a name an installed package registers under the `harness2.task_connectors` entry point, or a `module:Attr` import path to a connector factory |
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

## Connecting a task source

`harness2-intake` (`harness2/cli/intake.py`, also `python3 -m harness2.cli.intake`)
runs the tasks of any task source on one git repository. It builds a task
connector, takes up every task the connector offers (`run_intake`), runs each
rollout with OpenCode in its own git worktree of the repository, hands the
chosen run's diff to the connector, and prints one report per task it took up.
Each raw task goes through the normalizer named by `HARNESS2_NORMALIZER`
(`passthrough` by default, so the connector supplies the [TaskSpec](#taskspec-keys)
itself).

```bash
# the bundled local folder connector
harness2-intake --connector local --connector-opt root=/home/me/intake-root \
  --repo /home/me/proj --work-root /home/me/h2

# a connector from an installed package, registered under harness2.task_connectors
harness2-intake --connector my-tasks --connector-opt url=http://127.0.0.1:8000/mcp \
  --repo /home/me/proj --work-root /home/me/h2

# a connector that is not installed: the module:Attr import path of its factory
harness2-intake --connector mypkg.tasks:make_connector \
  --repo /home/me/proj --work-root /home/me/h2
```

### Choosing the connector

`--connector` takes a name, looked up in this order:

1. a built-in registration: only the bundled `local` folder connector
   ([above](#the-local-folder-connector));
2. an entry point of that name in the group `harness2.task_connectors`, which a
   package installed with pip registers (see
   [Registering the connector](#registering-the-connector));
3. for a name containing `:`, a `module:Attr` import path, for trying a connector
   without installing it.

Without `--connector`, the name comes from `HARNESS2_TASK_CONNECTOR`, else
`local`. The name resolves to a factory, which the CLI calls with keyword
arguments:

- every `--connector-opt KEY=VALUE` (repeatable; the value is a string), for the
  connector's own settings such as a URL or a start command;
- the context keywords `repo` (the `--repo` path, a string), `mode` (the first
  mode's on-disk token, `agg` or `seq`), `timeout` (the `--timeout` seconds, an
  int) and `locate` (a function that takes the summary `deliver` receives and
  returns `{"summary_json": <path>, "harness_dir": <path or None>}`, where the
  run's `summary.json` and the adopted harness lie).

A factory receives only the context keywords its signature names (all of them
when it takes `**kwargs`), and a `--connector-opt` of the same name wins. An
unknown name, a factory that rejects its keywords or returns something that is
not a task connector, and an optional capability of the wrong type are argument
errors.

### Arguments

| Argument | Default | Meaning |
|---|---|---|
| `--connector` | `$HARNESS2_TASK_CONNECTOR`, else `local` | A built-in name, an entry point name of `harness2.task_connectors`, or a `module:Attr` import path of a factory |
| `--connector-opt KEY=VALUE` | none | A keyword argument of the connector factory; repeatable |
| `--repo` | required | Absolute path of the root of the git work tree the tasks change |
| `--work-root` | required | The intake adapter's root; outside the `--repo` work tree |
| `--runs-root` | `<work-root>/runs` | Scheduler state |
| `--k` | `1` | Task-level recursion budget (`R = 4k` rollouts per task) |
| `--modes` | `agg` | `parallel` or `sequential` (`agg` / `seq` are the on-disk tokens), or a comma list; the report follows the first mode |
| `--seed`, `--tag` | none, empty | Enter the run namespace |
| `--model` | `config.SOLVER_MODEL_OC` | The OpenCode model of the agent; only with the default model connector |
| `--model-connector` | `$HARNESS2_MODEL_CONNECTOR`, else `env` | A built-in name (`env`), an entry point name of `harness2.model_connectors`, or a `module:Attr` import path of a factory. The improver of a connector without `run_session` is chosen once, at start-up |
| `--model-connector-opt KEY=VALUE` | none | A keyword argument of the model connector factory; repeatable |
| `--session-root DIR` | `<work-root>/_sessions` | Where the model connector's improver sessions get their working directories; a connector that runs the sessions must allow this directory |
| `--timeout` | `3600` | Seconds offered to the connector as its `timeout` keyword (for its own calls), and the timeout of the model channel the improver uses (proposer and selector) |

`--timeout` does not limit a rollout: the agent's run in each rollout is limited
by `IntakeAdapter.DEFAULT_TIMEOUT`, fixed at 3600 seconds.

`--repo` must be an absolute path to an existing directory that is the root of
its git work tree, not a directory inside it, and `--work-root` must lie outside
that work tree. `--k` and `--timeout` must be positive. Argument errors exit with
status 2 before any task is taken up.

### Worktrees and the diff

Each rollout gets its own detached git worktree at
`<work-root>/executions/<run_id>/worktree/`. Its base commit is the source HEAD
plus the tracked uncommitted changes and the untracked, not ignored files,
committed once inside the worktree. The git commands that make and fill a
worktree run with `core.hooksPath` pointed at an empty directory, so the source
repository's hooks (`pre-commit`, `post-checkout` and the like) do not run.
Repository location variables such as `GIT_DIR` and `GIT_WORK_TREE` are dropped
from git's environment, so every command works on the repository it names. When
a step after `git worktree add` fails, the worktree is removed and unregistered.

The agent works in the worktree, with the environment variables the connector
lists in `scrub_env` removed from its environment. Its deliverable is
`workspace/output/changes.diff`, the `git diff --binary` from the base commit,
without the paths the connector's workspace policy excludes. A rollout that
changed nothing writes no diff and has no deliverable. The worktrees are removed
when the run ends.

### What goes back to the source

The connector's `WorkspacePolicy` (`harness2.core.intake.WorkspacePolicy`)
decides what may go back:

- `exclude`: paths relative to the source root (files or directories) that are
  left out of the diff, such as the task source's own records kept in the
  repository;
- `guard`: called as `guard(worktree, source)` before the diff goes back; it
  returns None when the diff may go back, else the reason it may not. The agent
  can rewrite anything in the worktree, so a guard should trust only what it
  reads from the source.

The default policy leaves nothing out and checks nothing.

The harness does not change `--repo` itself: the chosen run's `workspace/output/`
goes to the connector's `deliver(task_id, deliverable_dir, summary)`, and the
connector decides. A connector that applies the diff calls
`harness2.core.worktree.apply_back(source, run_dir, guard=..., exclude=...)` with
its policy; `run_dir` is the chosen run's directory (`deliverable_dir.parent.parent`),
whose worktree still exists while `deliver` runs. `apply_back` calls the guard
first, refuses a diff that touches a path outside the source or under `exclude`,
then runs `git apply --check --binary` and `git apply --binary`. On any failure
the source is left as it was; its index and commits are never touched. When
`deliver` raises, the task is failed through the connector's `fail`. The bundled
`local` connector does not apply anything: it copies `changes.diff` into
`outbox/<id>/`.

### The report and the exit status

For every task this run delivered or failed, `harness2-intake` prints the text
the connector's `report(task_id)` returns, or, when the connector has no
`report` or it returns None, the line `<task_id>: <status>`. The reports are
separated by blank lines and taken before the connector is closed. A `report`
that raises falls back to the one line and adds an `error:` line on standard
error. The status follows the chosen run of the first mode:

| Status | When |
|---|---|
| `DONE` | The task was delivered and no error was recorded |
| `DONE_WITH_CONCERNS` | The task was delivered but errors were recorded during its run |
| `BLOCKED` | The task was failed through the connector's `fail`: it did not normalize, every base rollout failed, the chosen run left no deliverable, or `deliver` raised |

The exit status is 0 when every task taken up is `DONE`, 1 when any is not, when
the run failed, or when a `report` or the connector's `close` raised, and 2 for an
argument error. When the connector offers no task, `no task was taken up` goes
to standard error and the status is 0.

When the run fails part way (for example, a git or configuration error), the
reports of the tasks this run had already delivered or failed are still printed,
followed by one `error:` line, and the exit status is 1. The connector's `close`
runs in every case. A task that was delivered or failed is marked as handled
under `<work-root>/tasks/<task_id>/` and is not taken up again.

## Writing a task connector

A task connector is any object that meets the contract in
`harness2.core.intake`; harness2 needs no change to use it. These four methods
of the `TaskConnector` protocol are required:

| Method | Does |
|---|---|
| `poll() -> Iterable[RawTask]` | Returns the tasks to take up. A `RawTask` has `id`, `title`, `body`, `files` and `meta`; for the `passthrough` normalizer, put the TaskSpec dict in `meta["task_spec"]` or the body as JSON |
| `fetch_files(raw, dest) -> None` | Writes the task's attachments under `dest`; it may leave `dest` missing when there are none |
| `deliver(task_id, deliverable_dir, summary) -> None` | Takes the chosen run's `workspace/output/` and the task's summary dict |
| `fail(task_id, reason) -> None` | Takes a task that could not be delivered, with the reason |

### Optional capabilities

A connector may also offer these capabilities. `capabilities(connector)` reads
them with getattr and fills in the defaults; an attribute that is present but
None counts as missing, and a value of the wrong type raises TypeError naming the
capability.

| Capability | Kind | Default | Use |
|---|---|---|---|
| `workspace_policy` | Attribute: a `WorkspacePolicy(exclude=..., guard=...)` | `WorkspacePolicy()`: nothing left out, nothing checked | Paths kept out of the diff, and the check before the diff goes back ([above](#what-goes-back-to-the-source)) |
| `scrub_env` | Attribute: an iterable of environment variable names (not a single string) | `()` | Variables removed from the agent's environment, for example the task source's API key |
| `judge()` | Method returning a Judge or None; called once | None (no judge) | The scorer used after selection |
| `report(task_id)` | Method returning a string or None | None | The text printed for a task; without it, `<task_id>: <status>` |
| `close()` | Method | Does nothing | Releases connections and processes; the CLI calls it once at the end, also when the run fails |

### The judge

A Judge is a callable. `IntakeAdapter.score` calls it as
`judge(run_dir, task_id, criteria)` with the chosen run's directory, the task id,
and the task's private criteria, and it returns None (unscored) or a dict with
the keys the harness's scoring record reads:

| Key | Type | Meaning |
|---|---|---|
| `n_passed` | int | Criteria passed, `0 <= n_passed <= n_criteria` |
| `n_criteria` | int | Criteria checked |
| `all_pass` | bool | Every criterion passed; when missing, `n_passed == n_criteria` is used |

Other keys are kept as raw data. Selection never looks at scores, so the judge
is used only after selection, for post-hoc scoring. The judging step calls it
from several threads at once, so a judge that shares a connection must be safe to
call concurrently.

### Registering the connector

The connector is built by a factory: a callable that takes the connector options
as keyword arguments and returns the connector.

```python
from pathlib import Path

from harness2.core import worktree
from harness2.core.intake import RawTask, WorkspacePolicy


class MyConnector:
    workspace_policy = WorkspacePolicy(exclude=(".tasks",))
    scrub_env = ("MY_TASKS_API_KEY",)

    def __init__(self, *, url: str, repo: str, timeout: int):
        self.url, self.repo, self.timeout = url, Path(repo), timeout

    def poll(self): ...                      # -> list[RawTask]
    def fetch_files(self, raw, dest): ...

    def deliver(self, task_id, deliverable_dir, summary):
        run_dir = Path(deliverable_dir).parent.parent
        worktree.apply_back(
            self.repo,
            run_dir,
            guard=self.workspace_policy.guard,
            exclude=self.workspace_policy.exclude,
        )
        ...                                  # tell the task source the task is done

    def fail(self, task_id, reason): ...


def make_connector(*, url: str, repo: str, timeout: int = 60) -> MyConnector:
    return MyConnector(url=url, repo=repo, timeout=int(timeout))
```

A package makes the factory available by name with an entry point in the group
`harness2.task_connectors` in its `pyproject.toml`:

```toml
[project.entry-points."harness2.task_connectors"]
my-tasks = "my_package.connector:make_connector"
```

After `pip install`, `harness2-intake --connector my-tasks --connector-opt url=...`
uses it. Before installing, `--connector my_package.connector:make_connector`
loads the same factory.

### Contract tests

`harness2.testing.connector_contract` checks a connector against the contract
from your own tests. `check_connector(connector)`, `check_judge_result(result)`
and `check_fetched_files(dest)` raise AssertionError on a mismatch, and
`ConnectorContractTests` is a unittest mixin that runs them against the
connector your test builds: `poll()` yields only `RawTask`, `fetch_files` stays
under `dest`, the judge's result has the shape above, `report` returns a string
or None, and `close` does not raise.

```python
import unittest
from pathlib import Path

from harness2.testing.connector_contract import ConnectorContractTests
from my_package.connector import MyConnector


class MyConnectorContractTest(ConnectorContractTests, unittest.TestCase):
    def make_connector(self, tmp: Path):
        # Put at least one task where the connector finds it, so that
        # poll / fetch_files / judge run on a real task.
        return MyConnector(url="http://127.0.0.1:8000/mcp", repo=str(tmp), timeout=60)
```

List `unittest.TestCase` after the mixin. Each test calls `make_connector` with a
fresh temporary directory and closes the connector afterwards; override
`make_run_dir(tmp, task_id)` to give the judge a run directory that looks like a
finished run.

### The optional MCP plugin

A connector for a task server that speaks MCP can import
`harness2.plugins.mcp`, a small MCP client built only on the standard library
(nothing extra to install). harness2's core, CLI and benchmarks do not import it.

```python
from harness2.plugins.mcp import McpError, McpToolError, client_from

client = client_from(url="http://127.0.0.1:8000/mcp", timeout=60)
# or: client_from(command=["python", "my_task_server.py"], timeout=60)
try:
    tasks = client.call_tool("list_tasks", {"repo": "/home/me/proj"})
finally:
    client.close()
```

- `client_from(url=...)` returns an `HttpMcpClient` (Streamable HTTP) and
  `client_from(command=[...])` a `StdioMcpClient` (the server as a child process,
  one JSON-RPC message per line); give exactly one of them.
- `call_tool(name, arguments)` returns the tool's structured result as a dict. A
  result marked `isError` raises `McpToolError`; a transport or protocol failure
  raises `McpError`.
- HTTP goes only to the loopback addresses `127.0.0.1`, `localhost` and `::1`.
  The client ignores proxy settings in the environment and does not follow
  redirects; a redirect answer raises `McpError`.
- Each client runs its calls one after another under a lock, so one client may
  be shared between threads, for example by a judge that is called in parallel.

## Writing a memory connector

A memory connector holds the experience library for one domain. It replaces where
that library lives, and harness2 needs no change to use it. The default is `file`:
`FileConnector` (`harness2.core.library.Library`) keeps each domain's entries as
files under the library root.

### The contract

A connector meets the `MemoryConnector` protocol in `harness2/core/memory.py`.
These five members are required:

| Member | Does |
|---|---|
| `domain: str` | The domain whose entries this connector holds |
| `entries() -> list[str]` | The stored entries, by name and in ascending order |
| `retain(task_ids) -> None` | Keeps the entries for these task ids and drops every other entry |
| `add(task_id, harness, *, why, label, run_id, experience) -> Path \| None` | Stores one adopted harness and its experience; returns the entry directory, or None when it stores nothing |
| `context_dir() -> Path \| None` | The directory handed to an improver session, or None when the domain has nothing to show |

`recall(task_id, limit_chars, *, query="")` is optional. When a connector offers
it as a callable, `memory.recall` returns its text cut to `limit_chars`. `query`
is the current task's prompt; `memory.recall` passes it only to a recall that
accepts the keyword (a `query` parameter or `**kwargs`), so the two-argument
form keeps working. A connector without it, or a `limit_chars` of zero or less,
gives `""`.

`close()` is optional. When a connector defines it as a callable, `run_domain`
calls `memory.close` on every connector it built once the domain's tasks are
done, continuing past a `close` that fails and raising the first error after
they have all run; a connector without `close`, or with `close` set to None, is
left alone.

The connector is built by a factory: a callable called as `factory(root=Path,
domain=str, surface=SurfaceSpec)` with those three keyword arguments. A factory
that needs settings takes them as further keyword arguments: `--memory-opt
key=value` (repeatable) supplies each one, and every value arrives as a `str`.
`root`, `domain` and `surface` may not be overridden, and a key the factory
cannot accept is an argument error before the run starts. `SurfaceSpec` comes
from `harness2.core.tree` and describes the harness's file layout. A factory
whose result does not meet the protocol raises TypeError.

`memory.store` runs its checks before it calls `add`, so they hold whatever the
backend does. An incomplete experience (a `cue`, `lesson` and `evidence` that are
not all non-empty strings) stores nothing and returns None. Otherwise `store`
strips every recall block from the tree, checks the stripped tree and the
experience text for grader verdicts, and then calls `add`. A connector's `add` can
therefore assume a complete experience and a tree with no recall blocks.

### Choosing and registering the connector

The connector's name is the explicit name when one is given, else
`HARNESS2_MEMORY_CONNECTOR`, else the default `file`. `memory.resolve_factory`
turns the name into a factory in this order:

1. The built-in registrations (`memory.register`). By default only `file` is
   registered.
2. An entry point with the same name in the group `harness2.memory_connectors`.
3. A `module:Attr` import path, for a name that contains `:`. `Attr` may be a
   dotted chain.

A name found nowhere raises ValueError listing the registered and entry point
names.

A package makes its factory available by name with an entry point in its
`pyproject.toml`:

```toml
[project.entry-points."harness2.memory_connectors"]
my-memory = "my_package.memory:make_connector"
```

After `pip install`, set `HARNESS2_MEMORY_CONNECTOR=my-memory` to use it. Before
installing, `HARNESS2_MEMORY_CONNECTOR=my_package.memory:make_connector` loads the
same factory.

Settings for that factory come from `--memory-opt`, as in this made-up example:

```bash
harness2 ... --memory-opt url=http://127.0.0.1:9000 --memory-opt timeout=30
```

Each value is passed as a string keyword, so the factory above would declare
`url` and `timeout` parameters and receive `"http://127.0.0.1:9000"` and `"30"`.

### Scope and recall

`HARNESS2_MEMORY_SCOPE` is empty by default. Each run then derives its library
root from k, substrate and seed. A non-empty name makes the root
`<runs_root>/library/<benchmark>/scope-<name>`, whatever k, substrate and seed
are, and runs of that benchmark with the same name share it. With a named scope,
before each task
the library keeps this stream's preceding tasks and every entry that belongs to
no task of this stream, so entries written by other runs survive.

`HARNESS2_MEMORY_RECALL` is `0` by default. `1` injects each mode's recall into
that mode's starting harness, at the end of the surface's always-on file and
within its cap. The injected text is recorded on the summary, and a re-run reuses
the recorded text instead of recalling again. Any other value leaves recall
injection off.

When recall runs, the task's prompt is handed to the connector as `query`, so a
connector whose `recall` accepts it can pick the entries that relate to the
current task.

### Contract tests

`harness2.testing.memory_contract` checks a connector from your own tests.
`check_memory_connector(conn)` raises AssertionError naming a missing or invalid
member. `MemoryConnectorContractTests` is a unittest mixin that runs those checks
and a few more against the connector your test builds: a new connector has no
entries and a `context_dir()` that is None or an existing directory; two complete
experiences give their `library.slug` names in ascending order; `retain()` keeps
only the listed entries; storing the same task id twice keeps one entry;
`recall()` (if any) is empty before anything is stored, returns str afterwards,
and is cut to the requested limit; and an incomplete experience adds no entry.

```python
import unittest
from pathlib import Path

from harness2.testing.memory_contract import MemoryConnectorContractTests
from my_package.memory import MyConnector


class MyConnectorContractTest(MemoryConnectorContractTests, unittest.TestCase):
    def make_connector(self, root: Path, domain: str):
        return MyConnector(root=root, domain=domain)
```

List `unittest.TestCase` after the mixin, so unittest collects the mixin's tests.
Each test calls `make_connector` with a fresh temporary directory and the domain
`contract`.

## Writing a model connector

A model connector supplies the models of the executor and the improver, per role and
per task. It replaces where those models come from, and harness2 needs no change to
use it. The default is `env` (`EnvModelConnector` in `harness2/core/models.py`),
which returns the models the environment already selects.

### The contract

A connector meets the `ModelConnector` protocol in `harness2/core/models.py`. One
member is required:

| Member | Does |
|---|---|
| `routes(role, *, task_id, substrate) -> list[ModelChoice]` | Returns the candidates for `role` (`executor` or `improver`), best first. For the executor, `substrate` is the runner harness2 fixed and every candidate's `runner` must equal it; for the improver it is `""`. An empty list is allowed |

Four more members are optional. `capabilities(connector)` reads them with getattr and
fills in the defaults; an attribute that is present but None counts as missing, and
one that is present but not callable raises TypeError naming it. A connector that
defines `run_session` must also define `fences`, or `capabilities` raises TypeError:

| Capability | Does |
|---|---|
| `run_session(role, request) -> SessionResult` | Runs one session itself, on the runner and model of `request.choice` only; default None, so harness2 runs every session |
| `fences() -> Mapping[str, FenceDeclaration]` | The fence it applies to each runner it executes, by runner name; default `{}` |
| `record(role, task_id, choice, outcome) -> None` | Called once for each use of a candidate, with an `Outcome`. A skipped candidate is not a use, so `record` is not called for it. A `record` that raises is a warning on standard error, never a failed run; default None |
| `close() -> None` | Releases connections and processes; `harness2-intake` calls it once at the end, also when the run fails; the default does nothing |

These dataclasses carry the values:

| Type | Fields |
|---|---|
| `ModelChoice` | `runner` (the runner's name; for the executor, the substrate), `model`, and `effort` (default `""`) |
| `SessionRequest` | `cwd`, `mission`, `system`, `wall` (seconds), `label` and `choice` (the `ModelChoice` harness2 picked: a session runs on `choice.runner` and `choice.model` only) |
| `SessionResult` | `text`, `ok`, `cost_usd` (default `0.0`), `tokens` (a `Tokens` with `input` and `output`, each None when the backend does not report it), `wall_s` (default `0.0`), `error` (default `""`), `unavailable` (default False; true for a failure that should move on to the next candidate, such as a usage limit, an authentication failure, or a model or variant that does not exist) and `runner` (the runner that actually ran the session; empty means `choice.runner`, and a non-empty value that differs from it makes harness2 treat the result as a failure) |
| `Outcome` | What `record` receives for one use: `ok`, `unavailable`, `cost_usd`, `tokens`, `wall_s`, `error`, and `run_id` (executor) or `label` (improver) |
| `FenceDeclaration` | `tools` (the runner's tool names), `cwd_only` (no directory outside the session's working directory is added, so no `--add-dir`), `strict_mcp`, `no_prompts`, `restricted`, `permission_mode` and `dropped_env` |

A `SessionResult` that is `ok` with an empty `text` counts as a failure (`empty reply`).

### Executor and improver

For the executor, harness2 fixes the substrate: `harness2-intake` always runs
`opencode`, because the refinement changes that substrate's harness. The connector
chooses only the model and effort of each candidate, and their order. `routes` is
called once per task with the substrate. A candidate whose `runner` is not the
substrate is not run: it is kept as a summary row with `skipped_reason` (`runner
<runner> is not <substrate>`) and `n` 0.

A failure that means the model could not be used — a usage limit, an authentication
failure, or a model or variant that does not exist — moves the task's position to
the next candidate. Only the rollout whose candidate is still the current one moves
the position, so parallel rollouts that failed on the same candidate do not skip one.
When a next candidate exists, the failed rollout is retried once, in the same run
directory on the candidate now current, after its worktree is put back at the base
commit (`git reset --hard` and `git clean -fdx`); the failed attempt's
`events.1.jsonl`, `opencode.1.stderr` and `trajectory.1.md` stay beside the retry's
files. A second unavailable failure ends the rollout; the position has been moved
when a later candidate exists, so the remaining rollouts run that one. Any other
failure ends the rollout at once. When the candidates are used up, the rollout fails;
an empty list fails the task without running a model, with the reason handed to the
task connector's `fail`.

Only these failures count as unavailable: an error event whose numeric
`data.statusCode` is 401, 403, 429 or 529, whose `name` is one of
`ProviderAuthError`, `ProviderAuthenticationError`, `ProviderModelNotFoundError`,
`ProviderRateLimitError`, `ProviderQuotaExceededError` or
`ProviderUsageLimitError`, or whose `data.message`/`message` holds a specific phrase
such as `rate limit`, `quota`, `invalid api key`, `variant unavailable` or `model not
found`. The rest of the event (headers, provider ids, token limits, other numbers, a
status written as a string) is not inspected, so an ordinary failure is not mistaken
for an unusable model.

For the improver, a connector with `run_session` runs the sessions itself. harness2
hands it the working directory, the mission, the system prompt, the wall clock, a
label and the `choice`. The working directory is a fresh directory under
`--session-root` (default `<work-root>/_sessions`); `harness2-intake` prints that
root at start-up, and a connector that accepts only its own directories must allow
it. harness2 still prepares and caches the working directory, retries, copies the
context in and harvests the rewritten tree. It tries the candidates in order: a
candidate whose runner has no satisfied fence is skipped and noted (`skipped_reason`
`no fence`); an `unavailable` session moves on to the next candidate; any other
failure counts as the attempt's failure. When `SessionResult.runner` is not empty and
differs from `choice.runner`, the result is a failure (`connector ran <runner> instead
of <choice.runner>`), not an unavailable one.

A connector without `run_session` keeps a channel of ours: harness2 runs the improver
on the first improver candidate's runner and model through the existing `opencode` or
`claude_cli` channel, chosen once at start-up, not per task. Runner names map to
channel names through `models.RUNNER_CHANNELS` (`claude` to `claude_cli`, `opencode`
to `opencode`); a runner outside that table is an argument error. When the connector
offers no improver candidate, the environment's channel is used.

### Fences

A connector that runs sessions declares, for each runner it can execute, the fence it
applies, with `fences()`. harness2 holds a requirement for each runner it is willing
to hand a session to. Today only Claude Code has one:

| Runner | Requirement |
|---|---|
| `claude` | `tools` at most `Read`, `Edit`, `Write`, `Glob` and `Grep`; `cwd_only` (no directory outside the working directory is added, so no `--add-dir`); `strict_mcp`; `no_prompts`; `restricted`; `permission_mode` `acceptEdits`; and `dropped_env` including `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_BEDROCK_BASE_URL` and `CLAUDE_CODE_USE_BEDROCK` |

A declaration satisfies a requirement when it is a `FenceDeclaration` with
well-typed fields, its `tools` are a subset of the required tools, its `cwd_only`,
`strict_mcp`, `no_prompts` and `restricted` equal the required values, its
`permission_mode` equals the required one, and its `dropped_env` includes every
required name (`models.satisfies`; `models.fence_problems` lists what is missing). A
declaration of the wrong type is treated as not satisfying. A candidate for a runner
with no requirement, or with a fence that does not satisfy the requirement, is never
used for the improver; it is noted with `skipped_reason` `no fence` in the summary.

When a connector defines `run_session` but no declared runner satisfies the
requirement, `harness2-intake` stops at start-up with exit code 2 and the reasons
(`models.fence_report` names each runner and the item it misses). Adding a requirement
for another runner is a change in `FENCE_REQUIREMENTS`, not in the contract, so a
runner such as OpenCode or Codex can be accepted later without changing the
connectors. The declaration is trusted: harness2 does not inspect the connector's
processes.

### Choosing and registering the connector

The connector's name is the explicit name when one is given, else
`HARNESS2_MODEL_CONNECTOR`, else the default `env`. `models.resolve_factory` turns
the name into a factory in this order:

1. The built-in registrations (`models.register`). By default only `env` is
   registered.
2. An entry point with the same name in the group `harness2.model_connectors`.
3. A `module:Attr` import path, for a name that contains `:`. `Attr` may be a dotted
   chain.

A name found nowhere raises ValueError listing the registered and entry point names.

The factory is called with keyword arguments: every `--model-connector-opt KEY=VALUE`
(repeatable; each value arrives as a `str`), and nothing else. `check_options` checks
the keys against the factory's signature before the run starts, and a key the factory
cannot accept is an argument error. A factory whose result does not meet the protocol
raises TypeError.

A package makes its factory available by name with an entry point in its
`pyproject.toml`:

```toml
[project.entry-points."harness2.model_connectors"]
my-models = "my_package.models:make_connector"
```

After `pip install`, `harness2-intake --model-connector my-models` uses it, with the
connector's own settings passed as `--model-connector-opt KEY=VALUE`. Before
installing, `--model-connector my_package.models:make_connector` loads the same
factory. Without `--model-connector` the name comes from
`HARNESS2_MODEL_CONNECTOR`, else `env`. `--model` sets the executor model only with
the default connector: with any other connector the two are an argument error,
because the connector chooses the executor model.

### What is recorded

With a connector other than the default, `summary.json` gains `model_connector`, the
connector's name, and, when the run recorded one, `models`: one list per role that has
rows (`executor`, `improver`). The list holds the candidates in the order they were
first tried, each with `runner`, `model`, `effort`, `ok`, `unavailable`, `cost_usd`,
`tokens` (input and output) and `n`, the number of times it was tried. Only a
candidate that was skipped has `skipped_reason`, and its `n` stays 0; a candidate that
was tried never has that key. The connector's `record` receives each use as an
`Outcome` as it happens. With the default connector neither key is written, so
`summary.json` keeps its old shape.

### Contract tests

`harness2.testing.model_contract` checks a connector against the contract from your
own tests. `check_model_connector(conn)`,
`check_choices(choices, *, role, substrate)` and `check_session_result(result)` raise
AssertionError on a mismatch, and `ModelConnectorContractTests` is a unittest mixin
that runs them, and a few more, against the connector your test builds: `routes`
returns a list of `ModelChoice` for both roles (an empty list is fine) with the
executor's runner equal to the substrate; `fences()` (if any) passes
`models.fenced_runners`; `run_session` (if any) is called once, with a request from
`make_session_request(tmp, choice)` for the first improver candidate on the first
runner harness2 accepts, and passes `check_session_result` (skipped when there is no
such candidate); `record` (if any) accepts an `Outcome`; and `close` (if any) can be
called twice. `check_session_result` requires `text` and `runner` to be str, an `ok`
result to carry a non-empty `text` and not be `unavailable`, and a non-negative cost
and wall time.

```python
import unittest
from pathlib import Path

from harness2.core.models import ModelChoice, SessionRequest
from harness2.testing.model_contract import ModelConnectorContractTests
from my_package.models import MyConnector


class MyConnectorContractTest(ModelConnectorContractTests, unittest.TestCase):
    def make_connector(self):
        return MyConnector()

    def make_session_request(self, tmp: Path, choice: ModelChoice) -> SessionRequest:
        return SessionRequest(
            cwd=tmp,
            mission="say hello",
            system="be brief",
            wall=30,
            label="contract",
            choice=choice,
        )
```

List `unittest.TestCase` after the mixin, so unittest collects the mixin's tests. Each
test calls `make_connector()` with no arguments and closes the connector afterwards; a
connector without `run_session`, or a test that does not implement
`make_session_request`, skips the session check.
