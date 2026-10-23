# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Improver sessions with file access in an isolated context workspace.

OpenCode reads output/answer.md or the final message; the Claude CLI reads the
final message. Prompts use stdin, and empty replies raise an error.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from .spec import ChannelSpec

SYSTEM_ARG_MAX = 120_000

# The only tools a claude_cli session gets: file tools, no command or web tools.
CLAUDE_CLI_TOOLS = "Read,Edit,Write,Glob,Grep"
# The permission mode of a claude_cli session: edits are accepted without asking.
CLAUDE_CLI_PERMISSION_MODE = "acceptEdits"


MAX_CONCURRENCY = int(config.PROPOSER_CONCURRENCY)
_SEM = threading.BoundedSemaphore(MAX_CONCURRENCY) if MAX_CONCURRENCY > 0 else None


_DROP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
)


def _obj(d: dict, key: str) -> dict:
    """`d[key]` when it is a dict, else {}."""
    v = d.get(key)
    return v if isinstance(v, dict) else {}


class ProposerError(RuntimeError):
    """A call did not produce a usable reply. Never swallowed into an empty string."""

    def __init__(self, message: str, *, attempts: int = 0, cost_usd: float = 0.0):
        super().__init__(message)
        self.attempts = attempts
        self.cost_usd = cost_usd


@dataclass
class Reply:
    """One session: what came back, what it cost, and where the audit trail is."""

    label: str
    text: str
    ok: bool
    cost_usd: float = 0.0
    wall_s: float = 0.0
    attempts: int = 0
    cached: bool = False
    channel: str = ""
    error: str = ""
    audit: Path | None = None
    workspace: Path | None = None
    # The candidate a model connector ran this session on (empty on the other channels).
    runner: str = ""
    model: str = ""
    effort: str = ""

    def __bool__(self) -> bool:
        return self.ok


class ModelSession:
    """The improver side of a model connector: which task is current, which runners are fenced
    well enough, and what happened to each candidate (the `models.improver` rows of summary.json).

    The fenced runners are always the connector's declared fences checked against the
    requirements (`models.fenced_runners`). The connector must have `run_session`. `for_task`
    sets the task the next sessions belong to. `session_root` is the directory the connector
    channel creates its session working directories in (the system temporary directory when
    not given).

    For a caller that runs the tasks one after another (harness2-intake). Not for a caller that
    runs tasks in parallel: the current task is shared state.
    """

    def __init__(self, connector: Any, name: str = "", *, session_root: Path | str | None = None):
        from . import models

        caps = models.capabilities(connector)
        if caps.run_session is None:
            raise ValueError(f"model connector {name or connector!r} has no run_session")
        self.connector = connector
        self.name = name or models.DEFAULT
        self.fenced: dict = dict(models.fenced_runners(connector))
        self.session_root = Path(session_root) if session_root else Path(tempfile.gettempdir())
        self.run_session = caps.run_session
        self.record = caps.record
        self.task_id = ""
        self._lock = threading.Lock()
        self._records: dict[str, list[dict]] = {}

    def for_task(self, task_id: str) -> "ModelSession":
        """Make `task_id` the current task; returns self."""
        with self._lock:
            self.task_id = str(task_id)
        return self

    def candidates(self) -> list:
        """The connector's improver candidates for the current task, best first."""
        from . import models

        with self._lock:
            task_id = self.task_id
        return list(self.connector.routes(models.Role.IMPROVER, task_id=task_id, substrate=""))

    def _row(self, choice: Any) -> dict:
        rows = self._records.setdefault(self.task_id, [])
        key = (choice.runner, choice.model, choice.effort)
        for row in rows:
            if (row["runner"], row["model"], row["effort"]) == key:
                return row
        row = {
            "runner": choice.runner,
            "model": choice.model,
            "effort": choice.effort,
            "ok": False,
            "unavailable": False,
            "cost_usd": 0.0,
            "tokens": {"input": None, "output": None},
            "n": 0,
        }
        rows.append(row)
        return row

    def skip(self, choice: Any, reason: str) -> None:
        """Note a candidate that was not run, with why."""
        with self._lock:
            self._row(choice)["skipped_reason"] = reason

    def settle(
        self,
        choice: Any,
        label: str,
        *,
        ok: bool,
        unavailable: bool = False,
        cost_usd: float = 0.0,
        tokens: Any = None,
        wall_s: float = 0.0,
        error: str = "",
    ) -> None:
        """Note one session on `choice` and hand it to the connector's `record`."""
        from . import models

        outcome = models.Outcome(
            ok=bool(ok),
            unavailable=bool(unavailable),
            cost_usd=float(cost_usd or 0.0),
            tokens=tokens or models.Tokens(),
            wall_s=float(wall_s or 0.0),
            error=str(error or ""),
            label=label,
        )
        with self._lock:
            row = self._row(choice)
            row["n"] += 1
            row["ok"] = row["ok"] or outcome.ok
            row["unavailable"] = row["unavailable"] or outcome.unavailable
            row["cost_usd"] = round(row["cost_usd"] + outcome.cost_usd, 6)
            for name in ("input", "output"):
                value = getattr(outcome.tokens, name, None)
                if value is not None:
                    row["tokens"][name] = (row["tokens"][name] or 0) + int(value)
        if self.record is None:
            return
        try:
            self.record(models.Role.IMPROVER, self.task_id, choice, outcome)
        except Exception as exc:
            print(
                f"warning: model connector record failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    def records_for(self, task_id: str) -> list[dict]:
        """The summary rows of `task_id` (copies), in the order the candidates were first seen."""
        with self._lock:
            return [
                {**row, "tokens": dict(row.get("tokens") or {})}
                for row in self._records.get(task_id, [])
            ]


@dataclass
class Proposer:
    """Runs agentic sessions on one channel, with an audit trail and a cache.

    On the "connector" channel `models` (a ModelSession) is required and runs the sessions.
    """

    spec: ChannelSpec
    audit_dir: Path | None = None
    attempts: int = 2
    project: str = field(default_factory=lambda: config.VERTEX_PROJECT)
    region: str = field(default_factory=lambda: config.VERTEX_REGION)
    models: ModelSession | None = None
    _calls: list[Reply] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.spec.kind == "connector":
            if self.models is None:
                raise ValueError("the connector channel needs a ModelSession (models=...)")
            return
        if not self.spec.model.strip():
            raise ValueError("Set HARNESS2_IMPROVER_MODEL before starting model sessions")

    def env(self) -> dict[str, str]:
        e = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
        if self.spec.kind == "claude_cli":
            e.update(
                {
                    "CLAUDE_CODE_USE_VERTEX": "1",
                    "ANTHROPIC_VERTEX_PROJECT_ID": self.project,
                    "CLOUD_ML_REGION": self.region,
                    "ANTHROPIC_MODEL": self.spec.model,
                }
            )
        else:
            e.update(
                {
                    "GOOGLE_VERTEX_PROJECT": self.project,
                    "GOOGLE_VERTEX_LOCATION": self.region,
                    "VERTEXAI_LOCATION": self.region,
                }
            )
        return e

    def call(
        self,
        mission: str,
        context_dir: Path | str | None,
        label: str,
        *,
        system: str = "",
        timeout: int | None = None,
        max_attempts: int | None = None,
        harvest: str | None = None,
    ) -> Reply:
        """Run one session. Raises ProposerError if no usable reply after `attempts`.

        `harvest` names a subpath of the session's working directory to copy out before that
        directory is destroyed -- e.g. "context/harness", which is how an edited tree comes back.
        """

        cache_key = _call_fingerprint(label, mission, system, context_dir)
        cached = self._cached(label, cache_key, require_workspace=bool(harvest))
        if cached is not None:
            self._calls.append(cached)
            return cached

        wall = int(timeout or self.spec.timeout)
        tries = max(1, int(self.attempts if max_attempts is None else max_attempts))
        last = ""
        spent_usd = 0.0
        t0 = time.time()
        for attempt in range(1, tries + 1):
            if attempt > 1:
                time.sleep(10 * (attempt - 1))
            if self.spec.kind == "connector":
                assert self.models is not None
                self.models.session_root.mkdir(parents=True, exist_ok=True)
                tmp = Path(tempfile.mkdtemp(prefix="harness2-prop-", dir=self.models.session_root))
            else:
                tmp = Path(tempfile.mkdtemp(prefix="harness2-prop-"))
            try:
                if context_dir and Path(context_dir).is_dir():
                    shutil.copytree(context_dir, tmp / "context", dirs_exist_ok=True)
                choice = None
                with _SEM if _SEM is not None else contextlib.nullcontext():
                    if self.spec.kind == "connector":
                        text, cost, err, choice = self._connector(tmp, mission, system, wall, label)
                    else:
                        text, cost, err = (
                            self._claude_cli if self.spec.kind == "claude_cli" else self._opencode
                        )(tmp, mission, system, wall)
                spent_usd += cost
                if not text.strip():
                    last = err or "empty reply"
                    continue
                r = Reply(
                    label=label,
                    text=text,
                    ok=True,
                    cost_usd=spent_usd,
                    wall_s=round(time.time() - t0, 1),
                    attempts=attempt,
                    channel=self.spec.kind,
                )
                if choice is not None:
                    r.runner, r.model, r.effort = choice.runner, choice.model, choice.effort
                if harvest:
                    r.workspace = self._harvest(tmp, harvest, label, cache_key)
                    if r.workspace is None:
                        _drop_workspace(self._paths(label, cache_key))
                self._write_audit(r, mission, system, cache_key)
                self._calls.append(r)
                return r
            except subprocess.TimeoutExpired:
                last = f"timeout after {wall}s"
            except Exception as exc:
                # `_connector` carries the cost of the candidates that ran before it raised.
                spent_usd += float(getattr(exc, "harness2_cost_usd", 0.0) or 0.0)
                last = f"{type(exc).__name__}: {exc}"
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        r = Reply(
            label=label,
            text="",
            ok=False,
            wall_s=round(time.time() - t0, 1),
            attempts=tries,
            channel=self.spec.kind,
            error=last,
            cost_usd=spent_usd,
        )
        self._calls.append(r)
        self._write_audit(r, mission, system, cache_key)
        raise ProposerError(
            f"[{label}] {self.spec.kind}/{self.spec.model}: {last}",
            attempts=tries,
            cost_usd=spent_usd,
        )

    def _connector(
        self, cwd: Path, mission: str, system: str, wall: int, label: str
    ) -> tuple[str, float, str, Any]:
        """One attempt through the model connector: (text, cost, error, the candidate used).

        The connector's improver candidates are tried in order. One whose runner has no
        satisfied fence is skipped (and noted); `unavailable` moves on to the next; any other
        failure is this attempt's failure.
        """
        from . import models

        session = self.models
        assert session is not None
        cost = 0.0
        last = ""
        for choice in session.candidates():
            if choice.runner not in session.fenced:
                session.skip(choice, "no fence")
                continue
            request = models.SessionRequest(
                cwd=cwd, mission=mission, system=system, wall=wall, label=label, choice=choice
            )
            try:
                res = session.run_session(models.Role.IMPROVER, request)
            except BaseException as exc:
                session.settle(choice, label, ok=False, error=f"{type(exc).__name__}: {exc}")
                with contextlib.suppress(Exception):
                    exc.harness2_cost_usd = cost  # type: ignore[attr-defined]
                raise
            cost += float(res.cost_usd or 0.0)
            ran = res.runner or choice.runner
            error, ok, unavailable = res.error, res.ok, res.unavailable
            if ran != choice.runner:
                ok, unavailable = False, False
                error = f"connector ran {ran} instead of {choice.runner}"
            elif ok and not (res.text or "").strip():
                ok = False
                error = "empty reply"
            session.settle(
                choice,
                label,
                ok=ok,
                unavailable=unavailable,
                cost_usd=res.cost_usd,
                tokens=res.tokens,
                wall_s=res.wall_s,
                error=error,
            )
            if ok:
                return res.text, cost, error, choice
            if unavailable:
                last = error or "unavailable"
                continue
            return "", cost, error or "session failed", choice
        detail = f": {last}" if last else ""
        return "", cost, f"no fenced improver model{detail}", None

    def _claude_cli(
        self, cwd: Path, mission: str, system: str, wall: int
    ) -> tuple[str, float, str]:
        cli = shutil.which("claude") or "claude"
        # Fence the session like OPENCODE_PERMISSION does for _opencode: file tools only,
        # confined to cwd (the context is copied to cwd/context, so no --add-dir), no
        # command tools, no MCP servers, and anything that would prompt is denied.
        cmd = [
            cli,
            "--restricted",
            "--tools",
            CLAUDE_CLI_TOOLS,
            "--permission-mode",
            CLAUDE_CLI_PERMISSION_MODE,
            "--permission-prompts",
            "none",
            "--strict-mcp-config",
            "-p",
            "--output-format",
            "json",
            "--effort",
            self.spec.effort,
        ]
        stdin = mission
        if system:
            if len(system) > SYSTEM_ARG_MAX:
                stdin = f"# SYSTEM INSTRUCTIONS\n\n{system}\n\n# END\n\n{mission}"
                cmd += [
                    "--append-system-prompt",
                    "Follow the SYSTEM INSTRUCTIONS block at the top of the message.",
                ]
            else:
                cmd += ["--append-system-prompt", system]
        p = subprocess.run(
            cmd,
            input=stdin,
            cwd=cwd,
            env=self.env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=wall,
        )
        payload = _parse_json(p.stdout)
        text = str(payload.get("result") or payload.get("text") or "")
        err = ""
        if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
            err = f"CLI error: {payload.get('subtype')} {str(text)[:200]}"
            text = ""
        elif p.returncode != 0 and not text.strip():
            err = f"rc={p.returncode}: {(p.stderr or '')[-300:]}"
        return text, float(payload.get("total_cost_usd") or 0.0), err

    def _opencode(self, cwd: Path, mission: str, system: str, wall: int) -> tuple[str, float, str]:
        (cwd / "MISSION.md").write_text(
            (f"{system}\n\n---\n\n" if system else "") + mission, encoding="utf-8"
        )
        out = cwd / "output"
        out.mkdir(exist_ok=True)

        (cwd / "opencode.json").write_text(
            json.dumps({"agent": {"build": {"steps": self.spec.steps}}}), encoding="utf-8"
        )
        prompt = (
            "Read MISSION.md first. Do what it asks. Reply with the requested output as "
            "your final message AND save it verbatim to output/answer.md."
        )
        oc = _opencode_dir()

        env = self.env()
        env["OPENCODE_PERMISSION"] = json.dumps(
            {"external_directory": {"*": "deny", str(cwd): "allow", f"{cwd}/**": "allow"}}
        )
        env["XDG_DATA_HOME"] = str(cwd / ".ocdata")
        env["XDG_STATE_HOME"] = str(cwd / ".ocstate")

        cmd = [
            str(config.BUN),
            "run",
            "--cwd",
            str(oc / "packages" / "opencode"),
            "--conditions=browser",
            "src/index.ts",
            "run",
            prompt,
            "--dir",
            str(cwd),
            "--model",
            self.spec.model,
            "--format",
            "json",
        ]
        if self.spec.effort:
            cmd += ["--variant", self.spec.effort]
        p = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=wall,
        )
        cost = _opencode_cost(p.stdout)
        ans = out / "answer.md"
        if ans.is_file() and ans.read_text(errors="replace").strip():
            return ans.read_text(errors="replace"), cost, ""
        text, err = _opencode_stdout(p.stdout)
        if text.strip():
            return text, cost, ""
        tail = _strip_oc_banner(p.stderr or "").strip()[-300:]
        return "", cost, f"rc={p.returncode}: {err or tail or 'empty reply'}"

    def _harvest(self, tmp: Path, sub: str, label: str, cache_key: str = "") -> Path | None:
        """Copy `tmp/<sub>` somewhere durable before the session directory is removed."""
        src = tmp / sub
        if not src.is_dir():
            return None
        dest = Path(self.audit_dir or tempfile.mkdtemp(prefix="harness2-harvest-"))
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        if cache_key:
            safe = f"{safe}.{cache_key}"
        dest = dest / f"{safe}.workspace"
        shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src, dest)
        return dest

    def _paths(self, label: str, cache_key: str = "") -> dict[str, Path] | None:
        if not self.audit_dir:
            return None
        d = Path(self.audit_dir)
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
        if cache_key:
            safe = f"{safe}.{cache_key}"
        return {k: d / f"{safe}.{k}.md" for k in ("mission", "system", "response")} | {
            "meta": d / f"{safe}.meta.json",
            "workspace": d / f"{safe}.workspace",
        }

    def _cached(
        self, label: str, cache_key: str = "", *, require_workspace: bool = False
    ) -> Reply | None:
        p = self._paths(label, cache_key)
        if not p or not p["response"].is_file():
            return None
        if require_workspace and not p["workspace"].is_dir():
            return None
        txt = p["response"].read_text(encoding="utf-8", errors="replace")
        if not txt.strip():
            return None
        return Reply(
            label=label,
            text=txt,
            ok=True,
            cost_usd=0.0,
            cached=True,
            channel=self.spec.kind,
            audit=p["response"],
            workspace=p["workspace"] if p["workspace"].is_dir() else None,
        )

    def _write_audit(self, r: Reply, mission: str, system: str, cache_key: str = "") -> None:
        p = self._paths(r.label, cache_key)
        if not p:
            return
        p["mission"].write_text(mission, encoding="utf-8")
        if system:
            p["system"].write_text(system, encoding="utf-8")
        p["response"].write_text(r.text, encoding="utf-8")
        meta = {
            "label": r.label,
            "ok": r.ok,
            "channel": r.channel,
            "model": r.model or self.spec.model,
            "effort": r.effort or self.spec.effort,
            "attempts": r.attempts,
            "wall_s": r.wall_s,
            "cost_usd": r.cost_usd,
            "error": r.error,
        }
        if r.runner:
            meta["runner"] = r.runner
        p["meta"].write_text(
            json.dumps(meta, indent=2),
            encoding="utf-8",
        )
        r.audit = p["response"]


def _drop_workspace(paths: dict[str, Path] | None) -> None:
    """Remove a stale harvested workspace so a later cache read cannot serve it."""
    if paths:
        shutil.rmtree(paths["workspace"], ignore_errors=True)


def _opencode_dir() -> Path:
    """The upstream OpenCode checkout to run through bun: config.OPENCODE_DIR."""
    oc = config.OPENCODE_DIR
    if not (oc / "packages" / "opencode" / "src" / "index.ts").is_file():
        raise FileNotFoundError(
            f"no OpenCode checkout at {oc} (config.OPENCODE_DIR); run "
            f"scripts/setup_benchmark.py <benchmark> to clone the pinned commit there"
        )
    return oc


def _parse_json(stdout: str) -> dict:
    """The claude CLI's one JSON object, tolerating banners and trailing noise around it.

    Not for opencode stdout, which is a JSONL event stream: use `_opencode_stdout`.
    """
    s = (stdout or "").strip()
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        for line in reversed(s.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        i, j = s.find("{"), s.rfind("}")
        if 0 <= i < j:
            try:
                return json.loads(s[i : j + 1])
            except json.JSONDecodeError:
                pass
    return {"result": s}


_OC_BANNER = re.compile(
    r"^(?:Performing one time database migration[^\n]*|sqlite-migration:[^\n]*"
    r"|Database migration complete\.)[ \t]*$",
    re.M,
)


def _strip_oc_banner(s: str) -> str:
    """Drop the migration banner lines from OpenCode output. No-op when they are absent."""
    return _OC_BANNER.sub("", s or "")


def _opencode_stdout(stdout: str) -> tuple[str, str]:
    """Fold `opencode run --format json` into (final assistant text, error text).

    The stream is one raw event per line and assistant prose lives at part.text of a `text`
    event. All text events are joined, so a trailing terminator event cannot swallow the
    answer; `error` events become the error string. Plain-text stdout is returned as-is.
    """
    texts: list[str] = []
    errors: list[str] = []
    saw_json = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        saw_json = True
        part = _obj(ev, "part")
        ptype = part.get("type") or ev.get("type")
        if ptype == "text":
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                texts.append(t)
        elif ptype == "error":
            e = ev.get("error")
            if isinstance(e, dict):
                msg = str(_obj(e, "data").get("message") or e.get("name") or "").strip()
            else:
                msg = str(e or "").strip()
            if msg:
                errors.append(msg)
    if not saw_json:
        return _strip_oc_banner(stdout).strip(), ""
    return "\n\n".join(texts), "; ".join(errors)[-300:]


def _opencode_cost(stdout: str) -> float:
    """Sum `part.cost` over the step_finish events of an `opencode run --format json` stream."""
    total = 0.0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        part = _obj(ev, "part")
        if (part.get("type") or ev.get("type")) in ("step-finish", "step_finish"):
            c = part.get("cost")
            if isinstance(c, (int, float)):
                total += float(c)
    return round(total, 6)


def _call_fingerprint(label: str, mission: str, system: str, context_dir: Path | str | None) -> str:
    """Content-address one call, including its evidence and its mode/task workspace path.

    The resolved path is part of the key: the two modes can have byte-identical k=1 evidence
    and must still be independent draws.
    """
    h = hashlib.sha256()
    for value in (label, mission, system):
        h.update(value.encode("utf-8", errors="replace"))
        h.update(b"\0")
    if context_dir:
        root = Path(context_dir)
        h.update(str(root.resolve()).encode("utf-8", errors="replace"))
        h.update(b"\0")
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if not p.is_file() or "__pycache__" in p.parts:
                    continue
                h.update(str(p.relative_to(root)).encode("utf-8", errors="replace"))
                h.update(b"\0")
                try:
                    with p.open("rb") as fh:
                        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                            h.update(chunk)
                except OSError as exc:
                    h.update(f"<unreadable:{type(exc).__name__}:{exc}>".encode())
                h.update(b"\0")
    return h.hexdigest()[:16]
