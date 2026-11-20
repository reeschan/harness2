`harness2/core/runmark.py`: trial identity. A rollout is complete only when the code that saw
it return marks it, and the mark fingerprints the harness, so a rollout from another configuration
is never reused.
