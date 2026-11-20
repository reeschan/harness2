`harness2/config.py`: deployment settings and dependency versions. Values come from `.env` and
the shell (the shell wins). It offers `env`, `env_int`, `require`, the model routes (`ModelRoute`,
`route`, `validate_models`), the benchmark repository locations and `ConfigError`. It imports no
other harness2 module.
