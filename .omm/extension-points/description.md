The places where code outside harness2 plugs in. Each extension point is a contract in core,
found by name in the same order: a built-in registration (`register`), then an entry point of that
name in the point's entry point group, then, for a name containing `:`, a `module:Attr` import
path. Each contract except the rebutter has a contract check module in `harness2/testing/` that
authors run from their own tests.
