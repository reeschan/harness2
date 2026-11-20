`harness2/evaluators/__init__.py`: registers the bundled evaluators with `core.evaluate`. The
registration lives here because core is on a lower layer and must not import this package.
