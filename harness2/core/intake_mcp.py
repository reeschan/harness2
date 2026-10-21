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

"""A task connector over jev-mcp: PBI tasks in, verified diffs and five-line reports out.

`McpTaskConnector.poll` claims every queued PBI of one repository and turns each into a
RawTask whose `meta["task_spec"]` already is a TaskSpec dict (the passthrough normalizer
takes it as is). `deliver` runs the PBI's verification commands through jev-mcp's `verify`
in the chosen run's worktree, applies the run's diff back to the source repository after the
source's untouched-files check (`worktree.apply_back`), and submits the report with
`submit_result`. A failure goes back with `fail_task` as BLOCKED.

`McpVerifyJudge` is a judge for `IntakeAdapter.score`: it runs the same verification on a run
and returns the pass fraction. It is never used to select runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import intake_report, worktree
from .intake import RawTask
from .mcp_client import McpClient, McpError, McpToolError, client_from

RUNNER = "harness2"
DOMAIN = "pbi"
UNRESOLVED_MAX = 2_000

DELIVERABLE = {
    "what": "the code change that implements the PBI, collected from the repository worktree",
    "where": worktree.DIFF_NAME,
}
PROMPTS = {
    "work": "implement the PBI in the repository",
    "domain_word": "PBI",
    "deliverable_word": "change",
    "yardstick": "the PBI's acceptance criteria and verification commands",
}

Locate = Callable[[Mapping], Mapping[str, "Path | None"]]


def _task_prompt(claim: Mapping[str, Any]) -> str:
    """The request, the PBI, the rework note and worker.md, each under its own heading."""
    sections = [
        ("依頼文", claim.get("prompt")),
        ("PBI", claim.get("pbi_markdown")),
        ("差し戻し", claim.get("rework")),
        ("worker.md", claim.get("worker_md")),
    ]
    parts = [
        f"# {heading}\n\n{str(text).strip()}"
        for heading, text in sections
        if isinstance(text, str) and text.strip()
    ]
    return "\n\n".join(parts) + "\n"


def _jsonable(value: Any) -> Any:
    """`value` with every Path as str, tuples and sets as lists and keys as str."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _verify_counts(results: Any) -> tuple[list[Mapping], int, int]:
    """The result entries, how many have code 0, and how many there are."""
    if not isinstance(results, (list, tuple)):
        return [], 0, 0
    entries = list(results)
    passed = sum(1 for r in entries if isinstance(r, Mapping) and r.get("code") == 0)
    return [r for r in entries if isinstance(r, Mapping)], passed, len(entries)


def _unchanged_name(report: str) -> str:
    """The PBI name of a report name: `PBI-021.rework-1` -> `PBI-021`."""
    return report.split(".", 1)[0]


class McpTaskConnector:
    """A TaskConnector for the PBIs of one repository, through a jev-mcp client."""

    def __init__(
        self,
        client: McpClient,
        repo: str | Path,
        *,
        mode: str = "agg",
        locate: Locate | None = None,
        runner: str = RUNNER,
    ) -> None:
        self.client = client
        self.repo = str(repo)
        self.mode = mode
        self.locate = locate
        self.runner = runner

    def _raw_task(self, title: str, report: str, claim: Mapping[str, Any]) -> RawTask:
        spec = {
            "task_id": report,
            "domain": DOMAIN,
            "prompt": _task_prompt(claim),
            "files": [],
            "deliverable": dict(DELIVERABLE),
            "prompts": dict(PROMPTS),
            "private_criteria": [],
        }
        return RawTask(
            id=report,
            title=title,
            body=str(claim.get("prompt") or ""),
            meta={
                "task_spec": spec,
                "pbi": claim.get("pbi"),
                "verify_commands": claim.get("verify_commands"),
            },
        )

    def poll(self) -> list[RawTask]:
        """Claim every queued task; a task another runner took first is skipped.

        Returns a list (not a generator) so every claim of this poll happens before the caller
        looks at the first task.
        """
        listed = self.client.call_tool("list_tasks", {"repo": self.repo})
        out: list[RawTask] = []
        for task in listed.get("tasks") or []:
            if not isinstance(task, Mapping) or task.get("state") != "queued":
                continue
            report = str(task.get("report") or "")
            if not report:
                continue
            try:
                claim = self.client.call_tool(
                    "claim_task", {"repo": self.repo, "report": report, "runner": self.runner}
                )
            except McpToolError:
                continue
            out.append(self._raw_task(str(task.get("title") or ""), report, claim))
        return out

    def fetch_files(self, raw: RawTask, dest: Path) -> None:
        """A PBI task has no attachments: only `dest` is made."""
        Path(dest).mkdir(parents=True, exist_ok=True)

    def _locations(self, summary: Mapping) -> Mapping[str, Path | None]:
        if self.locate is None:
            return {"summary_json": None, "harness_dir": None}
        return self.locate(summary)

    def deliver(self, task_id: str, deliverable_dir: Path, summary: Mapping) -> None:
        """Verify the chosen run, apply its diff to the source, and submit the report.

        A failed verification still applies and submits (the designer judges the result).
        A WorktreeError from apply_back propagates, so the caller fails the task instead.
        """
        run_dir = Path(deliverable_dir).parent.parent
        verified = self.client.call_tool(
            "verify",
            {
                "repo": self.repo,
                "report": task_id,
                "workdir": str(run_dir / worktree.WORKTREE_DIRNAME),
                "name": f"{task_id}.harness2",
            },
        )
        results, passed, total = _verify_counts(verified.get("results"))
        changed = worktree.apply_back(
            Path(self.repo), run_dir, unchanged_name=_unchanged_name(task_id)
        )
        status = intake_report.report_status(summary, mode=self.mode)
        locations = self._locations(summary)
        errors = summary.get("errors") or []
        unresolved = "; ".join(str(e) for e in errors)[:UNRESOLVED_MAX]
        details = intake_report.render_details(
            summary, mode=self.mode, locations=locations, verify=results, changed=changed
        )
        self.client.call_tool(
            "submit_result",
            {
                "repo": self.repo,
                "report": task_id,
                "status": status,
                "passed": passed,
                "total": total,
                "changed": list(changed),
                "unresolved": unresolved,
                "details": details,
                "artifacts": _jsonable(summary),
            },
        )

    def fail(self, task_id: str, reason: str) -> None:
        self.client.call_tool(
            "fail_task",
            {
                "repo": self.repo,
                "report": task_id,
                "status": intake_report.BLOCKED,
                "reason": reason or "failed",
            },
        )


class McpVerifyJudge:
    """A judge for `IntakeAdapter.score`: the pass fraction of jev-mcp's `verify` on a run."""

    def __init__(self, client: McpClient, repo: str | Path) -> None:
        self.client = client
        self.repo = str(repo)

    def __call__(self, run_dir: Path, task_id: str, criteria: Sequence[str]) -> dict | None:
        try:
            verified = self.client.call_tool(
                "verify",
                {
                    "repo": self.repo,
                    "report": task_id,
                    "workdir": str(Path(run_dir) / worktree.WORKTREE_DIRNAME),
                    "name": f"{task_id}.judge",
                },
            )
        except McpError:
            return None
        results = verified.get("results")
        _, passed, total = _verify_counts(results)
        return {
            "passed": passed,
            "total": total,
            "frac": passed / total if total else 0.0,
            "results": results,
        }


def connector_from_options(
    *,
    repo: str | Path,
    url: str | None = None,
    command: Sequence[str] | None = None,
    mode: str = "agg",
    locate: Locate | None = None,
    timeout: float = 60,
) -> McpTaskConnector:
    """The jev-mcp connector over an HTTP (`url`) or stdio (`command`) client."""
    client = client_from(url=url, command=command, timeout=timeout)
    return McpTaskConnector(client, repo, mode=mode, locate=locate)
