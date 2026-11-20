`harness2/core/models.py`: model connectors, found by name (built-in registration, entry point
group `harness2.model_connectors`, or `module:Attr`). A connector returns `ModelChoice` candidates
per role and may run whole sessions behind a declared fence.
