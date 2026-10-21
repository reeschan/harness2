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

"""Deployment settings and dependency versions. Shell values override .env."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent if (PKG.parent / "pyproject.toml").is_file() else Path.cwd()
THIRD_PARTY = ROOT / "third_party"
METHOD_VERSION = 2

DEPENDENCIES = ROOT / "dependencies"


class ConfigError(RuntimeError):
    """A required deployment value is missing; the message carries the remediation."""


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Load KEY=VALUE lines from `path` into os.environ with setdefault (the shell wins).

    Accepts blank lines, `#` comments, an optional `export ` prefix, and single- or
    double-quoted values. A missing or unreadable file is not an error.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not all(c.isalnum() or c == "_" for c in key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


load_dotenv()


def env(name: str, default=None):
    """Env override where a set-but-empty variable is the same as an unset one."""
    return os.environ.get(name) or default


def env_int(name: str, default: int) -> int:
    v = env(name)
    if v is None:
        return int(default)
    try:
        return int(v)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {v!r}") from e


def require(name: str, *, needed_by: str) -> str:
    """The value of `name`, or ConfigError with the remediation line when it is unset/empty."""
    v = env(name)
    if not v:
        raise ConfigError(
            f"{name} is required for {needed_by}; set it in .env or the shell (see .env.example)"
        )
    return str(v)


RUNS = Path(env("HARNESS2_RUNS_DIR", ROOT / "runs")).expanduser().resolve()
# Non-empty: sweeps share one library root per benchmark across runs (scope-<value>).
MEMORY_SCOPE = env("HARNESS2_MEMORY_SCOPE", "")
# "1": inject each mode's library recall into the starting harness (off by default).
MEMORY_RECALL = env("HARNESS2_MEMORY_RECALL", "0") == "1"
VERTEX_PROJECT = env("GOOGLE_CLOUD_PROJECT", env("HARNESS2_VERTEX_PROJECT", ""))
VERTEX_REGION = env("GOOGLE_CLOUD_LOCATION", "global")
if VERTEX_PROJECT:
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", VERTEX_PROJECT)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", VERTEX_REGION)

_vertex_host = (
    "aiplatform.googleapis.com"
    if VERTEX_REGION == "global"
    else f"{VERTEX_REGION}-aiplatform.googleapis.com"
)
VERTEX_OPENAI_URL = (
    f"https://{_vertex_host}/v1/projects/{VERTEX_PROJECT}"
    f"/locations/{VERTEX_REGION}/endpoints/openapi"
)


@dataclass(frozen=True)
class ModelRoute:
    """How one role reaches its model. `auth_env` names the key's env variable, never its value."""

    role: str
    provider: str
    model: str
    endpoint: str = ""
    auth_env: str = ""


def _opencode_provider(model: str) -> str:
    """The provider of an OpenCode `provider/model` ID ("" when there is no `/`)."""
    return model.split("/", 1)[0] if "/" in model else ""


def _build_roles() -> dict[str, ModelRoute]:
    """Role -> route, from the environment as it stands after load_dotenv() and VERTEX_*."""
    solver_oc = env("HARNESS2_SOLVER_MODEL", "")
    solver_cx = env("HARNESS2_CODEX_MODEL", solver_oc.split("/", 1)[-1])
    improver = env("HARNESS2_IMPROVER_MODEL", solver_oc)
    # claude_cli takes HARNESS2_IMPROVER_MODEL as a bare ID; it never falls back to the solver.
    improver_cli = (
        env("HARNESS2_IMPROVER_MODEL", "")
        if env("HARNESS2_IMPROVER_CHANNEL", "opencode") == "claude_cli"
        else improver
    )
    judge = env("HARNESS2_JUDGE_MODEL", "")
    # JB decides the google/ prefix from HARNESS2_JUDGE_BASE_URL itself, not from the endpoint
    # (the default Vertex endpoint still gets the prefix).
    jb_judge = (
        "google/" + judge
        if judge.startswith("gemini") and not env("HARNESS2_JUDGE_BASE_URL")
        else judge
    )
    lab_judge = "vertex/" + judge if judge.startswith(("claude", "gemini")) else judge
    routes = (
        ModelRoute("executor.oc", _opencode_provider(solver_oc), solver_oc),
        ModelRoute(
            "executor.cx",
            "harness2",
            solver_cx,
            endpoint=env("HARNESS2_CODEX_BASE_URL", ""),
            auth_env="HARNESS2_CODEX_API_KEY",
        ),
        ModelRoute("improver.opencode", _opencode_provider(improver), improver),
        ModelRoute("improver.claude_cli", "claude-code-vertex", improver_cli),
        ModelRoute(
            "judge.jb",
            "openai-compatible",
            jb_judge,
            endpoint=env("HARNESS2_JUDGE_BASE_URL", VERTEX_OPENAI_URL if VERTEX_PROJECT else ""),
            auth_env="HARNESS2_JUDGE_API_KEY",
        ),
        ModelRoute("judge.lab", _opencode_provider(lab_judge), lab_judge),
        # The bare judge ID; the WorkBuddy routes file prefixes it with google/.
        ModelRoute("judge.wb", "workbuddy", judge),
        ModelRoute("normalizer", "", ""),
    )
    return {r.role: r for r in routes}


ROLES: dict[str, ModelRoute] = _build_roles()


def route(role: str) -> ModelRoute:
    """The route for `role`; ValueError listing the known roles when it is unknown."""
    try:
        return ROLES[role]
    except KeyError:
        raise ValueError(f"unknown role {role!r}; known roles: {', '.join(ROLES)}") from None


SOLVER_MODEL_OC = ROLES["executor.oc"].model
SOLVER_MODEL_CX = ROLES["executor.cx"].model
IMPROVER_MODEL = ROLES["improver.opencode"].model
JUDGE_MODEL = ROLES["judge.wb"].model

DEPENDENCY_PINS = {
    "job-bench-eval": "fa03a17f9ff662f136798a274e10011c3aa00fd0",
    "workbuddy-bench": "b516950be5b56eb3be406c2f76ee1c5111dcb57f",
    "harvey-labs": "b58a28ae9fde5ee4b810e53b4fd0a1915325a35d",
    "opencode": "23fb5e0516c99ac04a1aa46c193efda2e1b9bb24",
}
DEPENDENCY_ORIGINS = {
    "job-bench-eval": "https://github.com/Job-Bench/job-bench-eval.git",
    "workbuddy-bench": "https://github.com/Tencent/workbuddy-bench.git",
    "harvey-labs": "https://github.com/harveyai/harvey-labs.git",
    "opencode": "https://github.com/anomalyco/opencode.git",
}


def jb_repo() -> Path:
    return DEPENDENCIES / "job-bench-eval"


def wb_repo() -> Path:
    return DEPENDENCIES / "workbuddy-bench"


def lab_repo() -> Path:
    return DEPENDENCIES / "harvey-labs"


OPENCODE_DIR = DEPENDENCIES / "opencode"
BUN = DEPENDENCIES / "tools" / "node_modules" / ".bin" / "bun"
LAB_PYTHON = lab_repo() / ".venv" / "bin" / "python"
LAB_JUDGE_MODEL = ROLES["judge.lab"].model
LAB_DELIV_CAP = 60000
JB_JUDGE_PYTHON = jb_repo() / ".venv" / "bin" / "python"
JB_JUDGE_MODEL = ROLES["judge.jb"].model
JB_JUDGE_API_BASE = ROLES["judge.jb"].endpoint
JB_JUDGE_API_KEY = env("HARNESS2_JUDGE_API_KEY", "")
JB_JUDGE_REASONING_EFFORT = "low"
JB_REPORTING_JUDGE_DETAILS = ""
JB_OBS_TRUNC = 50000

CODEX_VERSION = "0.144.6"
CODEX_DIST = str(DEPENDENCIES / "codex" / "dist")
CODEX_BASE_URL = ROLES["executor.cx"].endpoint
CODEX_CONTAINER_URL = env("HARNESS2_CODEX_CONTAINER_URL", CODEX_BASE_URL)
VERTEX_PROXY_KEY = env("HARNESS2_CODEX_API_KEY", "")
CODEX_HOME_DIR = DEPENDENCIES / "codex" / "home"

WB_SOLVER_MODEL = "harness2/solver"
WB_WEB_JUDGE = "harness2/judge"
WB_RENDER_PY = sys.executable
WB_OC_HARNESS_VER = "1.14.18"
WB_CX_HARNESS_VER = CODEX_VERSION
WB_MAX_JOBS = {"oc": 1, "cx": 1}
WB_MAX_TRIALS = {"oc": 2, "cx": 2}
WB_MODEL_CONNECTION = "local_proxy"

IMPROVER_CHANNELS = ("opencode", "claude_cli")
DEFAULT_PROPOSER_CHANNEL = env("HARNESS2_IMPROVER_CHANNEL", "opencode")
PROPOSER_MODEL_BY_CHANNEL = {
    "opencode": ROLES["improver.opencode"].model,
    "claude_cli": ROLES["improver.claude_cli"].model,
}
PHASE_WIDTH = 2
TRANSPORT_ATTEMPTS = 2
PROPOSER_CONCURRENCY = 2


def validate_models(*, judge: bool = False) -> None:
    channel = DEFAULT_PROPOSER_CHANNEL
    if channel not in IMPROVER_CHANNELS:
        raise ConfigError(
            f"HARNESS2_IMPROVER_CHANNEL must be one of {IMPROVER_CHANNELS}, got {channel!r}"
        )
    require("HARNESS2_SOLVER_MODEL", needed_by="executor rollouts")
    if "/" not in SOLVER_MODEL_OC or (channel == "opencode" and "/" not in IMPROVER_MODEL):
        raise ConfigError("Executor and improver models must use OpenCode's provider/model syntax")
    if channel == "claude_cli":
        require("HARNESS2_IMPROVER_MODEL", needed_by="the claude_cli improver channel")
        if "/" in ROLES["improver.claude_cli"].model:
            raise ConfigError(
                "The claude_cli improver channel takes a bare model ID without a provider "
                "prefix in HARNESS2_IMPROVER_MODEL"
            )
    if any(m.startswith("google-vertex") for m in (SOLVER_MODEL_OC, IMPROVER_MODEL)):
        if not VERTEX_PROJECT:
            raise ConfigError("Set GOOGLE_CLOUD_PROJECT for Vertex AI")
    if judge:
        require("HARNESS2_JUDGE_MODEL", needed_by="benchmark judging")
