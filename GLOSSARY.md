# harness2

harness2 runs agent tasks, records what happened as traces, and evaluates those records. This glossary names the concepts of the evaluation side and of the code's layers.

## Evaluation

**Trace**:
The event record of a task run: spans that open and close, and point events inside them.
_Avoid_: log (for harness2's own record)

**Run graph**:
The graph of one or more traces, in which a node is a span or a point event and an edge says which span contained it.
_Avoid_: trace graph, call graph

**Trace source**:
A named reader that turns the records of some system into trace events, so that its runs enter the run graph.
_Avoid_: adapter, importer, bridge

**System**:
The program whose runs a trace describes. harness2 is one system; a run from any other system is an external run.
_Avoid_: tool, producer

**External run**:
A run in the run graph whose trace came from a system other than harness2.
_Avoid_: foreign run, imported run

**Viewpoint**:
One angle of evaluation, with a short name and the event kinds it reads.
_Avoid_: dimension, category, aspect

**Built-in viewpoint**:
One of the viewpoints harness2 itself defines: cost, budget, effect, safety, reproducibility and generalization.

**Declared viewpoint**:
A viewpoint an evaluator defines for itself, outside the built-in ones.
_Avoid_: custom viewpoint, plugin viewpoint

**Evaluator**:
A named reader of the run graph that reports findings for the viewpoints it declares.
_Avoid_: checker, judge

**Finding**:
One problem an evaluator reports, with the evidence that shows it.
_Avoid_: issue, warning

**Evidence**:
A pointer from a finding to a node of the run graph.

## Code

**Layer**:
A group of harness2's packages that may import from the layers below it and never from the layers above it.
_Avoid_: tier, level
