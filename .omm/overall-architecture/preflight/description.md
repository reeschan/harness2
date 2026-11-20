`harness2/preflight.py`: benchmark dependency and runtime readiness checks (`check`,
`require_ready`). It imports `install` and the OpenCode executor, so it sits in the benchmarks
layer; `harness2` (run_sweep) and `harness2-check` call it.
