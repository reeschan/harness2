`harness2/core/trace.py`: `TraceEvent`, `Kind`, the `Tracer` and spans, and trace sinks found by
name (entry point group `harness2.trace_sinks`). The tracer replaces long strings by a sha256 and
a length, so prompts, artifacts and harness contents stay out of the trace.
