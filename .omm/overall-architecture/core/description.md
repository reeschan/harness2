`harness2/core/`: the method, independent of any benchmark and any executor. Per-task harness
adaptation over a c1..c8 component tree, two recursion modes (parallel and sequential) at an
identical budget, blind answer-free selection, a per-domain experience library, the traces a run
leaves behind, the run graph and its evaluation, and task intake from task connectors.
Core imports only from the base layer.
