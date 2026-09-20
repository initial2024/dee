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

Direct local GGUF is the primary local path when configured. `xiaoyu-router local serve`, `local status`, `local models`, `local select <model_id>`, `local start`, `local stop`, `local restart`, and `local smoke` manage a loopback `llama-server` without downloading models. The default discovery directories reuse LM Studio downloads at `%USERPROFILE%\\.lmstudio\\models` and `%USERPROFILE%\\.cache\\lm-studio\\models`; only those directories plus explicitly configured directories are scanned. `local serve` safely auto-selects only when no model is selected and returns the loopback OpenAI-compatible endpoint. `local profile history_chongzhen` shows the history-simulation boundary; `local run-profile history_chongzhen --prompt "..."` is a local smoke/demo, not a Codex task entry. `scripts\\download-bonsai-model.ps1` never downloads anything automatically, including when passed `-ConfirmDownload`. LM Studio at `http://127.0.0.1:1234/v1` remains an optional fallback when no Direct Local backend is configured. API project snippets are redacted and `.env`/private-key files are denied before external transmission.

Provider and model policy is a hard constraint. Use `--allow-provider`, `--deny-provider`, `--allow-model`, `--deny-model`, `--api-model`, `--local-model`, `--no-api`, and `--no-local` with `auto`, `delegate`, or `ask`. A denied provider is never contacted and an explicit model override cannot bypass a deny. `xiaoyu-router models` shows discovered versus eligible models without printing credentials.

## 小羽 Local Agent

Local Agent 是独立于 Codex 官方 Agent 的本地执行层。它可以把本地模型、DeepSeek Web Bridge 或已显式 allowlist 的外部 Provider 作为“分析大脑”，但默认只生成脱敏计划，不调用 Codex 官方 Agent，也不代表任何文件或命令已经执行。

```powershell
xiaoyu-router agent plan --task "只读检查当前项目" --brain-provider local-light
xiaoyu-router agent plan --task "分析当前项目并给出计划" --brain-provider deepseek-head --invoke-brain
xiaoyu-router agent readonly --plan <plan-id>
xiaoyu-router agent draft-patch --plan <plan-id> --confirm
xiaoyu-router agent test --plan <plan-id> --test python-unittest --confirm
xiaoyu-router agent commit --plan <plan-id> --file src/example.py --message "local agent confirmed change" --confirm
```

`apply`、`test` 和 `commit` 没有 `--confirm` 时都会拒绝；补丁必须先通过 `git apply --check`，测试命令只能来自白名单，提交只允许本地 commit，永远不会执行 `git push`。删除全部文件、部署、生产、防火墙、LAN/公网、凭据、验证码/风控绕过等任务统一返回 `LOCAL_AGENT_HIGH_RISK_STOP`。默认记录只包含计划 ID、Provider、风险、动作、状态和文件名，不保存 prompt、response、API key、Authorization、Cookie、Token 或 storageState。

`--invoke-brain` 是显式开关；没有它时不会调用任何模型。启用后只生成分析文本，不会修改文件、运行命令或提交。DeepSeek 只能经已运行的 loopback Browser Bridge；外部 API 必须处于本地 allowlist 的 `ENABLED` 状态；Provider 输出不会写入默认 ledger。
