`harness2/testing/trace_source_contract.py`: checks a trace source against
`core/trace_source.py`: `read(location)` returns events and warnings, each event a `SourcedEvent`
with its `Origin` (source name, file, 1-based line); each external run has one root `task` span
naming the `system` (not `harness2`), other kinds use the `<system>.` namespace and no built-in
kinds appear. Offers `contract_events`, `check_trace_source`, `TraceSourceContractTests` and the
fake `FixedTraceSource`.
