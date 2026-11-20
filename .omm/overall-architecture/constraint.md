- A module imports only from its own unit and from units of lower layers; units in one layer never
  import each other. `tests/test_layers.py` checks this for every `.py` file under `harness2/`,
  including lazy and relative imports.
- A new module directly under `harness2/` must be given a layer in `LAYERS`; a new module inside a
  unit package belongs to that unit automatically.
- Nothing in harness2 imports `harness2.plugins`.
