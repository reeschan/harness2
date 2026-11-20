`harness2/core/schedule.py`: namespacing (`ns`, `Namespace`), the seeded stream order, and the
drivers `run_task`, `run_domain` and `run_sweep`. `TaskResult` is what a task writes to
`summary.json`. It opens the tracer for each task and wires the library, memory, recall,
recursion, selection and runmark modules together.
