- `harness2/core/` holds about thirty modules; scheduling, traces, evaluation and intake share one
  unit, so the layer check cannot catch an unwanted import between them.
- The bundled evaluators register themselves when `harness2.evaluators` is imported; a caller that
  skips that import relies on the installed entry point instead.
