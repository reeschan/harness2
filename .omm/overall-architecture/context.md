The package has no runtime dependencies (`dependencies = []` in pyproject.toml); optional
extras add document rendering and a model endpoint bridge. Benchmarks and executors are
installed separately by `harness2-setup` (harness2/install.py). The method in `harness2/core/`
reaches benchmarks only through the seam in `core/spec.py` and executors only through the
`Substrate` interface, so a new benchmark or executor is a new module in `benchmarks/` or
`substrate/`, not a change to core.
