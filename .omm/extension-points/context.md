Built-in implementations: the `local` task connector (`core/intake_local.py`), the on-disk
library as memory, an environment-based model connector (`EnvModelConnector`), the `FileSink`
trace sink, the `trace` trace source (`TraceFiles`, harness2's own `trace.jsonl`), and the `rules`
evaluator (`harness2/evaluators/`). Normalizers (`core/normalize.py`) are chosen by name too, from
built-in registrations and the `HARNESS2_NORMALIZER` variable.
