`harness2/demo.py`: `DemoAdapter` and `DemoProposer`, a deterministic local adapter with local
editors that exercises the real recursion and library workflow without a model or a benchmark.
It imports `core` and the OpenCode executor's surface, so it sits in the benchmarks layer.
`harness2-demo` (harness2/cli/demo.py) runs it.
