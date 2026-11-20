`harness2/core/evalgraph.py`: the evaluation graph, a DAG of evaluator, rebut and aggregate nodes
read from a JSON config. Rebutters are found by name (entry point group `harness2.rebutters`);
`run` returns the aggregated findings, the dropped or refused ones and the errors.
