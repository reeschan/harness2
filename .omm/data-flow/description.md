How the record of a run becomes findings. A harness2 task run emits trace events through the
`Tracer` into a trace sink (by default `trace.jsonl` beside the task's `summary.json`). The
records of any other system are read by a trace source into the same trace events, each carrying
the file and line it came from. `graph.build` / `graph.load_runs` join both into one run graph;
the evaluation graph runs evaluators, rebutters and one aggregate node over it; `harness2-evaluate`
writes the result as files.
