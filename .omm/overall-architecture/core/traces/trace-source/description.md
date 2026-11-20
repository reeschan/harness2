`harness2/core/trace_source.py`: trace sources, named readers that turn the records of some system
into trace events. A source has one member, `read(location)`, which returns `SourcedEvent`s (a
`TraceEvent` plus its `Origin`: the source name, the file and the 1-based line) and warnings.
Sources are found by name like the evaluators: `register`, the entry point group
`harness2.trace_sources`, or `module:Attr`. The built-in source `trace` (`TraceFiles`) reads
harness2's own `trace.jsonl` files. `check` keeps the events of external runs to the rules: one
root `task` span naming the `system`, every other kind under the `<system>.` namespace, and no
built-in kinds.
