# Codex AI Router V1

Independent companion tool for selecting Codex supervision, a local LM Studio model, and any OpenAI-compatible API. It never attempts to call the current Codex session as an API: `CODEX_ACTION_REQUIRED` means the current Codex must act.

## Quick start

```powershell
./scripts/install_router.ps1
xiaoyu-router doctor
xiaoyu-router auto "summarize this README"
```

Supported modes: `CODEX_ONLY`, `API_ONLY`, `LOCAL_ONLY`, `CODEX_API`, `CODEX_LOCAL`, `API_LOCAL`, and `AUTO_TRIAD` (default). The router uses deterministic risk rules first; helpers cannot perform high/critical-risk writes, deployments, credential changes, destructive Git operations, or data migrations.

Copy `config/config.example.yaml` for local reference only. API configuration is exclusively environment-based: `XIAOYU_CODER_API_BASE`, `XIAOYU_CODER_API_MODEL`, and `XIAOYU_CODER_API_KEY`. `configure-api.ps1` stores values in the Windows per-user environment variable store, which is accessible to processes running as that user; remove them with `[Environment]::SetEnvironmentVariable('XIAOYU_CODER_API_KEY', $null, 'User')` (and likewise for base/model). Never commit a key.

Direct local GGUF is the primary local path when configured. `xiaoyu-router local status`, `local models`, `local select <model_id>`, `local start`, `local stop`, `local restart`, and `local smoke` manage a loopback `llama-server` without downloading models. The default discovery directories reuse LM Studio downloads at `%USERPROFILE%\\.lmstudio\\models` and `%USERPROFILE%\\.cache\\lm-studio\\models`; only those directories plus explicitly configured directories are scanned. LM Studio at `http://127.0.0.1:1234/v1` remains an optional fallback when no Direct Local backend is configured. API project snippets are redacted and `.env`/private-key files are denied before external transmission.

Provider and model policy is a hard constraint. Use `--allow-provider`, `--deny-provider`, `--allow-model`, `--deny-model`, `--api-model`, `--local-model`, `--no-api`, and `--no-local` with `auto`, `delegate`, or `ask`. A denied provider is never contacted and an explicit model override cannot bypass a deny. `xiaoyu-router models` shows discovered versus eligible models without printing credentials.
