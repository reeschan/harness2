- The trace keeps bodies out: prompts, artifacts and harness contents are not stored, and long
  strings become a sha256 and a length.
- An event of an external run that breaks the trace source rules is skipped with one warning; a
  line that cannot be read is a warning, not an error.
