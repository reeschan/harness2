`harness2/core/trace_file.py`: the default trace sink `FileSink`, one JSON object per line in
`trace.jsonl` appended under an exclusive lock, and `read`, which skips torn lines.
