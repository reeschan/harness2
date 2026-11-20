harness2 is a Python package that adapts an agent harness per task: it runs rollouts of a task on an
executor, lets an improver edit a c1..c8 component tree of the harness, selects a final output
blind to grading, keeps a per-domain experience library, records traces of every run, and
evaluates the runs afterwards.

This perspective shows every package and every module directly under `harness2/`:

- base: `harness2/__init__.py` and `harness2/config.py` (deployment settings).
- core: `harness2/core/`, the method itself, independent of any benchmark or executor.
- evaluators, substrate, testing, install: built on core and independent of each other.
- benchmarks, demo, preflight: built on the executors.
- cli: the `harness2-*` commands.
- plugins: optional building blocks for code outside harness2; nothing in harness2 imports them.

The arrows are imports. They always point to a lower layer of the table in
`tests/test_layers.py` (see the `dependency-map` perspective).
