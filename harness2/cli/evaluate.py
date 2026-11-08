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

"""Evaluate the runs under one directory and write the findings as files.

Reads every `trace.jsonl` under `--runs` with the `summary.json` next to it, runs the
evaluation graph over the resulting run graph, and writes the aggregated findings, the
findings dropped or refused, the errors, the graph warnings, and the number of traces and
runs read. Each viewpoint also gets its own pre-aggregation list under
`findings/<viewpoint>.json`; a rerun removes the files of viewpoints it no longer has.

    harness2-evaluate --runs runs/jb/adapt/jb-oc-agg1

`--evaluators` names the evaluators of the default graph (one node per name, all wired to one
aggregate node); `--graph` takes a JSON graph config instead and wins over `--evaluators`.
The default `--out` is `<runs>/evaluation`.

Scores are kept on the grading side: without `--allow-scores` no `judge.*` node enters the
graph and `scores` stays `None`. With it, the score files are still used only when the graph
config also has `allow_scores: true`, so a config that does not opt in never sees them.

Exit codes: 0 when the run finished, findings or not; 2 for a bad argument, a bad graph config,
an unknown evaluator name or a directory without any trace.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..core import evalgraph, trace
from ..core import evaluate as EV
from ..core import graph as G
from ..core.evaluate import EvalRequest
from . import _common as C


def _evaluator_names(raw: str) -> list[str]:
    """The comma list as names; an empty list raises ValueError."""
    names = [name.strip() for name in raw.split(",") if name.strip()]
    if not names:
        raise ValueError(f"--evaluators takes a comma list of names, got {raw!r}")
    return names


def _config(args: argparse.Namespace) -> tuple[dict, bool]:
    """(graph config, whether scores may be read) from the arguments.

    With `--graph` the file is read and checked; scores need the flag and the config's
    `allow_scores`. Otherwise the default graph of `--evaluators` is built, and the flag
    alone decides.
    """
    if args.graph:
        config = evalgraph.load_config(args.graph)
        allow_scores = args.allow_scores and bool(config.get("allow_scores", False))
        return config, allow_scores
    config = evalgraph.default_config(
        _evaluator_names(args.evaluators), allow_scores=args.allow_scores
    )
    return config, args.allow_scores


def _scores(level: G.Graph) -> dict[str, list[dict]]:
    """The attrs of each run's `judge.*` nodes, keyed by the run id of its TASK node."""
    scores: dict[str, list[dict]] = {run_id: [] for run_id in level.runs()}
    for key in sorted(level.nodes):
        node = level.nodes[key]
        if node.kind.startswith(trace.JUDGE_PREFIX) and node.run_id in scores:
            scores[node.run_id].append(dict(node.attrs))
    return scores


def _write_json(path: Path, data: Any) -> None:
    """Write one JSON file with the repository's CLI conventions."""
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="harness2-evaluate", description=__doc__, formatter_class=C.HelpFormatter
    )
    p.add_argument("--runs", required=True, metavar="DIR", help="directory of the task runs")
    p.add_argument(
        "--evaluators",
        default="rules",
        metavar="a,b",
        help="comma list of evaluator names for the default graph",
    )
    p.add_argument(
        "--graph",
        default=None,
        metavar="FILE",
        help="JSON graph config; wins over --evaluators",
    )
    p.add_argument(
        "--out",
        default=None,
        metavar="DIR",
        help="output directory; defaults to <runs>/evaluation",
    )
    p.add_argument(
        "--allow-scores",
        action="store_true",
        help="let the evaluation see the judge events and the scores",
    )
    args = p.parse_args(argv)

    runs = Path(args.runs)
    out = Path(args.out) if args.out else runs / "evaluation"
    try:
        config, allow_scores = _config(args)
    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2

    paths = sorted(path for path in runs.rglob(G.TRACE_NAME) if path.is_file())
    if not paths:
        print(f"error: no {G.TRACE_NAME} under {runs}", file=sys.stderr, flush=True)
        return 2
    level, summaries = G.load_runs(runs, include_judge=allow_scores)
    scores = _scores(level) if allow_scores else None
    request = EvalRequest(
        graph=level,
        summaries=summaries,
        viewpoints=EV.VIEWPOINTS,
        scores=scores,
    )
    try:
        result = evalgraph.run(config, request)
    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2

    for warning in level.warnings:
        print(f"warning: {warning}", file=sys.stderr, flush=True)
    payload = dict(
        result.to_dict(),
        warnings=list(level.warnings),
        traces=len(paths),
        runs=len(level.runs()),
    )
    findings_path = out / "findings.json"
    out.mkdir(parents=True, exist_ok=True)
    _write_json(findings_path, payload)
    per_viewpoint = out / "findings"
    per_viewpoint.mkdir(parents=True, exist_ok=True)
    for stale in per_viewpoint.glob("*.json"):
        if stale.is_file() and stale.stem not in result.by_viewpoint:
            stale.unlink()
    for name in sorted(result.by_viewpoint):
        items = [item.to_dict() for item in result.by_viewpoint[name]]
        _write_json(per_viewpoint / f"{name}.json", items)
    print(f"wrote {findings_path}: {len(result.findings)} finding(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
