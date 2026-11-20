The layers of harness2, exactly as the `LAYERS` table in `tests/test_layers.py` defines them,
lowest first:

1. base: `harness2`, `harness2.config`
2. core: `harness2.core`
3. evaluators, substrate and testing: `harness2.evaluators`, `harness2.substrate`,
   `harness2.testing`, `harness2.install`
4. benchmarks: `harness2.benchmarks`, `harness2.demo`, `harness2.preflight`
5. cli: `harness2.cli`
6. plugins: `harness2.plugins`

A unit may import from its own unit and from the units of any lower layer. Two units in the same
layer never import each other.
