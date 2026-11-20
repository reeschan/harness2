- An import from a lower layer to a higher one, or between two units of one layer, fails
  `tests/test_layers.py`.
- Every module directly under `harness2/` must be in `LAYERS`; a module inside a unit package
  belongs to that unit.
- This perspective must equal `LAYERS`: change both together.
