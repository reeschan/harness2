`harness2/install.py`: repeatable installation of benchmark tools and release-owned runner
overlays (clone, patch, overlays, executor configuration, data download). It reads only
`harness2.config`, so it sits in the same layer as evaluators, substrate and testing, and
`harness2-setup`, `preflight` and the benchmark adapters use it.
