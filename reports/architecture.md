# Architecture

The CLI constructs a Router that combines deterministic classifier and risk policy with LM Studio and OpenAI-compatible provider adapters. Codex is the current supervisor, not a provider process. Helper writes are designed for isolated Git worktrees; shared write trees are prohibited.
