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

"""A generic status rule a connector can use when it builds its report.

Pure functions over the dict `schedule.TaskResult.to_dict()` writes to summary.json:
`report_status` decides the task's state (DONE / DONE_WITH_CONCERNS / BLOCKED) for one
mode, and `summary_locations` finds summary.json and the adopted harness the way
`schedule.run_task` names them. How the report itself is shaped is up to each connector.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from .schedule import Namespace

DONE = "DONE"
DONE_WITH_CONCERNS = "DONE_WITH_CONCERNS"
BLOCKED = "BLOCKED"

BASE_FAILED = "base-failed"


def _selection(summary: Mapping, mode: str) -> Mapping:
    sel = (summary.get("selections") or {}).get(mode)
    return sel if isinstance(sel, Mapping) else {}


def _has_output(run_dir: Path) -> bool:
    """True iff run_dir/workspace/output holds at least one regular file that is not a symlink."""
    out = run_dir / "workspace" / "output"
    if out.is_symlink() or not out.is_dir():
        return False
    for root, _dirs, files in os.walk(out, followlinks=False):
        for name in files:
            p = Path(root) / name
            if not p.is_symlink() and p.is_file():
                return True
    return False


def report_status(summary: Mapping, *, mode: str) -> str:
    """The task's state for `mode`'s outcome on this summary.

    A chosen run with a deliverable is DONE (DONE_WITH_CONCERNS if any error is recorded).
    Otherwise, when every base rollout failed (`run_task` marks this with status
    "base-failed" and runs no mode), the task is BLOCKED; anything else ran but left no
    deliverable on the chosen run and is DONE_WITH_CONCERNS.
    """
    chosen = str(_selection(summary, mode).get("chosen_run_dir") or "")
    if chosen and _has_output(Path(chosen)):
        return DONE_WITH_CONCERNS if summary.get("errors") else DONE
    if summary.get("status") == BASE_FAILED:
        return BLOCKED
    return DONE_WITH_CONCERNS


def summary_locations(
    runs_root: Path,
    summary: Mapping,
    *,
    bench: str,
    substrate: str,
    mode: str,
    tag: str = "",
) -> dict[str, Path | None]:
    """Where `schedule.run_task` put summary.json and the adopted harness of `mode`."""
    task_id = str(summary["task_id"])
    k = int(summary["k"])
    seed = summary.get("seed")
    seed = int(seed) if seed is not None else None
    base_ns = Namespace(bench, substrate, "agg", k, seed, tag)
    label = str(_selection(summary, mode).get("chosen_label") or "")
    harness_dir = None
    if label:
        mode_ns = Namespace(bench, substrate, mode, k, seed, tag)
        harness_dir = mode_ns.task_dir(runs_root, task_id) / "harnesses" / label
    return {
        "summary_json": base_ns.task_dir(runs_root, task_id) / "summary.json",
        "harness_dir": harness_dir,
    }
