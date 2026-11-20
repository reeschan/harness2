- Core imports only `harness2` and `harness2.config`; it never imports evaluators, substrate,
  testing, benchmarks, cli or plugins (the bundled evaluators register themselves from
  `harness2/evaluators/__init__.py` for that reason).
- No prompt in core names a rubric, a score or a judge; `judge.py` is imported by nothing in the
  method.
