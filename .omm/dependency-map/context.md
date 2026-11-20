`tests/test_layers.py` holds the one table of layers and checks every `.py` file under
`harness2/` with `ast`, so lazy imports inside functions and relative imports count. GLOSSARY.md
defines a layer. The imports found by the scan that placed each unit:

- `harness2.install` reads only `harness2.config`.
- `harness2.demo` imports `core` and `substrate.opencode`; `harness2.preflight` imports `install`
  and `substrate.opencode`; so both sit above substrate and install, beside the benchmark adapters.
- `harness2.benchmarks` imports `core`, `substrate`, `install` and `config`.
- `harness2.cli` imports `benchmarks`, `demo`, `preflight`, `evaluators`, `core`, `install` and
  `config`.
- `harness2.plugins` imports nothing from harness2 and nothing in harness2 imports it.
