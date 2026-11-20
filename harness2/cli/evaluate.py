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

"""Evaluate task runs and the records of other systems, and write the findings as files.

Reads every `trace.jsonl` under `--runs` with the `summary.json` next to it, and the records
each `--source <name>=<location>` names through that trace source, into one run graph. Runs
the evaluation graph over it and writes the aggregated findings, the findings dropped or
refused, the errors, the graph warnings, the number of traces read (`traces`), the number of
runs (`runs`, external runs included) and the number of runs each trace source gave
(`sources`, by the trace source name of the nodes). Each viewpoint also gets its own
pre-aggregation list under `findings/<viewpoint>.json`; a rerun removes the files of
viewpoints it no longer has.

    harness2-evaluate --runs runs/jb/adapt/jb-oc-agg1
    harness2-evaluate --source orchestrator=records/ --runs runs/jb/adapt/jb-oc-agg1

A trace source name is looked up like an evaluator name: a registration, an entry point of
the group `harness2.trace_sources`, then `module:Attr`. `--source` may be given more than
once, and `--runs DIR` means the same as `--source trace=DIR` (the built-in trace source).
At least one of `--runs` and `--source` is needed.

`--evaluators` names the evaluators of the default graph (one node per name, all wired to one
aggregate node); `--graph` takes a JSON graph config instead and wins over `--evaluators`.
The default `--out` is `<runs>/evaluation`, or `<location>/evaluation` of the first
`--source` when `--runs` is not given; a location that is a file puts it next to the file.

A trace source that fails on its location does not stop the run: an OSError, or any other
exception its `read` raises (a result that is not an `(events, warnings)` pair included), is
one warning and that source gives no runs.

Scores are kept on the grading side: without `--allow-scores` no `judge.*` node enters the
graph and `scores` stays `None`. With it, the score files are still used only when the graph
config also has `allow_scores: true`, so a config that does not opt in never sees them.

Exit codes: 0 when the run finished, findings or not; 2 for a bad argument, a bad graph config,
an unknown evaluator or trace source name, or inputs without any trace and any trace source
run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .. import evaluators  # noqa: F401  (registers the bundled evaluators, `rules` among them)
from ..core import evalgraph, trace
from ..core import evaluate as EV
from ..core import graph as G
from ..core import trace_source as TS
from ..core.evaluate import EvalRequest
from . import _common as C


def _source_spec(raw: str) -> tuple[str, str]:
    """`NAME=LOCATION` as (name, location); the first `=` splits, and both must be non-empty."""
    name, sep, location = raw.partition("=")
    name = name.strip()
    if not sep or not name or not location:
        raise ValueError(f"--source takes NAME=LOCATION, got {raw!r}")
    return name, location


def _inputs(
    runs: Path | None, specs: list[tuple[str, str]]
) -> tuple[list[Path], list[TS.SourceRead]]:
    """(trace roots, trace source results) of `--runs` and the `--source` arguments.

    Every name is resolved before anything is read, so an unknown name stops the run first. A
    `--source` of the built-in `trace` adds its location to the roots, the same as `--runs`.
    A location a trace source cannot open (OSError) becomes a warning of its result, and so does
    any other exception of its `read`, the TypeError of a result that is not an
    `(events, warnings)` pair included: a broken input or a broken trace source must not stop
    the evaluation, and exit 2 stays for the arguments, the config and the names.
    """
    roots = [] if runs is None else [runs]
    resolved = [(name, location, TS.trace_source_for(name)) for name, location in specs]
    reads: list[TS.SourceRead] = []
    for name, location, source in resolved:
        if isinstance(source, TS.TraceFiles):
            roots.append(Path(location))
            continue
        try:
            reads.append(TS.read_from(source, location))
        except OSError as exc:
            reads.append(TS.SourceRead([], [f"cannot read {location} with {name}: {exc}"]))
        except Exception as exc:
            reads.append(TS.SourceRead([], [f"trace source {name} failed on {location}: {exc}"]))
    return roots, reads


def _external_runs(level: G.Graph) -> dict[str, int]:
    """The number of external runs by the trace source name of their `task` node."""
    counts: dict[str, int] = {}
    for node in level.runs().values():
        if isinstance(node.source, TS.Origin):
            counts[node.source.name] = counts.get(node.source.name, 0) + 1
    return counts


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
    p.add_argument(
        "--runs",
        default=None,
        metavar="DIR",
        help="directory of the task runs; the same as --source trace=DIR",
    )
    p.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="NAME=LOCATION",
        help="read LOCATION with the trace source NAME; may be repeated",
    )
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
        help=(
            "output directory; defaults to <runs>/evaluation, else <first source>/evaluation"
            " (next to the source when it is a file)"
        ),
    )
    p.add_argument(
        "--allow-scores",
        action="store_true",
        help="let the evaluation see the judge events and the scores",
    )
    args = p.parse_args(argv)

    try:
        specs = [_source_spec(raw) for raw in args.source]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2
    if args.runs is None and not specs:
        print("error: give --runs DIR or --source NAME=LOCATION", file=sys.stderr, flush=True)
        return 2
    runs = None if args.runs is None else Path(args.runs)
    if args.out:
        out = Path(args.out)
    elif runs is not None:
        out = runs / "evaluation"
    else:
        first = Path(specs[0][1])
        out = (first.parent if first.is_file() else first) / "evaluation"
    try:
        config, allow_scores = _config(args)
        roots, reads = _inputs(runs, specs)
    except (ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2

    paths = TS.trace_paths(*roots)
    level, summaries = G.load_runs(roots, sources=reads, include_judge=allow_scores)
    external = _external_runs(level)
    if not paths and not external:
        where = ", ".join(str(root) for root in roots)
        if reads:
            message = f"no {G.TRACE_NAME} under {where or '(no --runs)'} and no trace source run"
        else:
            message = f"no {G.TRACE_NAME} under {where}"
        for warning in level.warnings:
            print(f"warning: {warning}", file=sys.stderr, flush=True)
        print(f"error: {message}", file=sys.stderr, flush=True)
        return 2
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
        sources=external,
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
