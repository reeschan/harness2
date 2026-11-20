`harness2/benchmarks/`: benchmark adapters. Each implements the `BenchmarkAdapter` seam of
`core/spec.py` for one benchmark (task discovery, prompts, launcher bodies, deliverables and
scores) and runs rollouts on an executor from `harness2/substrate/`. The intake adapter treats the
tasks of a task connector as one benchmark.
