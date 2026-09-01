# Reference

Information-oriented documentation: exhaustive, dry, accurate. Read it like a dictionary.

- **[Configuration](configuration.md)**: every `config.toml` key, what it accepts, which keys apply
  live without a restart, and who may write each one. Its short form is
  [`config.example.toml`](https://github.com/saxman/kokua/blob/main/src/kokua/config.example.toml), one
  line per key, which `kokua config init` scaffolds for you.

Kokua is a thin application over [AIMU](https://saxman.info/aimu/), so the primitives it wires together
are documented there: the [model matrix](https://saxman.info/aimu/reference/model-matrix/), the
[environment variables](https://saxman.info/aimu/reference/env-vars/) Kokua inherits, and the
[API reference](https://saxman.info/aimu/reference/).
