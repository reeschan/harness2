`harness2/core/graph.py`: `build` and `load_runs` read `trace.jsonl` files and the events of trace
sources into a `Graph`: a span becomes one `Node`, a point event its own node, and `parent_id`
becomes a `contains` edge. Each node keeps its source (the trace file and line, or the trace
source origin), so evidence leads back to the original record.
