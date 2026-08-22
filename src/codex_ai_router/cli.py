from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from .router import Router
from .task import Mode
from .model_policy import SelectionPolicy
from .configuration import load_config
from . import provider_config
from .providers import LMStudioProvider, OpenAICompatibleProvider, GroqProvider
from .provider_setup import default_display_name, suggested_provider_id, suggested_provider_type
from .providers.model_discovery import codex_profile_requires_bearer_auth
from .policy import FastLocalPolicy
from .network import NetworkMode
from .server import RouterResponsesServer, RouterService
from .providers.local_backend import LocalBackend, ManagedLlamaCppBackend, load_local_backend_config, save_local_backend_config, local_backend_config_path
from .providers.runtime_models import RuntimeModelState, probe_model, text_candidates
from .providers.model_states import model_state_report
from .handoff import compact_handoff
from .codex_integration import install_xiaoyu_router_provider, same_thread_provider_switch_support
from .delegation import explain_delegation
from .groq_diagnostics import diagnose as diagnose_groq, live_smoke as live_smoke_groq
from .tools_policy import VALID_POLICIES, load_policy, policy_path, save_policy
from .deepseek_modes import load_probe_state, probe_deepseek_modes, select_deepseek_mode
from .local_agent import BRAIN_PROVIDERS, LocalAgent, LocalAgentError
from .agent_api import AGENT_API_BASE


def emit(data): print(json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, dict) else data.to_json())


def agent_api_request(path: str, payload: dict | None = None) -> dict:
    """Call only the fixed loopback Agent API; never accept a remote URL."""
    target = AGENT_API_BASE + path
    body = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = Request(target, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
            result = json.loads(raw) if raw else {}
            return result if isinstance(result, dict) else {"status": "ERROR", "error_code": "INVALID_RESPONSE"}
    except HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8")
            result = json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        result.setdefault("error_code", "AGENT_API_HTTP_ERROR")
        result["http_status"] = exc.code
        return result
    except (URLError, TimeoutError, OSError):
        return {"status": "ERROR", "error_code": "AGENT_API_UNAVAILABLE", "loopback_only": "YES"}


def provider_diagnostic(provider_id: str, entry: dict, remote_status: str) -> dict:
    """Non-secret operational classification for the provider table and CLI."""
    base_url = str(entry.get("base_url", "")).rstrip("/")
    is_groq = "api.groq.com/openai/v1" in base_url.lower()
    return {
        "provider_id": provider_id,
        "base_url": base_url,
        "wire_api": entry.get("wire_api"),
        "auth_style": entry.get("model_discovery_auth_style", "inherit"),
        "api_key_env": entry.get("api_key_env", ""),
        "api_key_configured": "YES" if entry.get("api_key_env") and __import__("os").getenv(entry["api_key_env"]) else "NO",
        "custom_header_names": sorted(entry.get("headers", {}).keys()) if isinstance(entry.get("headers"), dict) else [],
        "status": "GROQ_AUTH_OR_PERMISSION_ERROR" if is_groq and remote_status in {"HTTP_401", "HTTP_403"} else remote_status,
        "GROQ_CUSTOM_HEADER_NOT_REQUIRED": "YES" if is_groq and not entry.get("headers") else "NO" if is_groq else "NOT_APPLICABLE",
    }


def add_policy_args(command):
    command.add_argument("--api-model")
    command.add_argument("--local-model")
    command.add_argument("--allow-model", action="append", default=[])
    command.add_argument("--deny-model", action="append", default=[])
    command.add_argument("--allow-provider", action="append", default=[])
    command.add_argument("--deny-provider", action="append", default=[])
    command.add_argument("--no-api", action="store_true")
    command.add_argument("--no-local", action="store_true")


def main() -> None:
    parser = argparse.ArgumentParser(prog="xiaoyu-router")
    parser.add_argument("--config", type=Path, help="optional policy/config YAML")
    parser.add_argument("--offline", action="store_true", help="disable every external API request for this invocation")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status", "models"):
        sub.add_parser(name)
    classify = sub.add_parser("classify"); classify.add_argument("task")
    ask = sub.add_parser("ask"); ask.add_argument("--provider", choices=("local", "api"), required=True); add_policy_args(ask); ask.add_argument("task")
    delegate = sub.add_parser("delegate"); delegate.add_argument("--mode", choices=[m.value for m in Mode]); add_policy_args(delegate); delegate.add_argument("task")
    auto = sub.add_parser("auto"); add_policy_args(auto); auto.add_argument("task")
    review = sub.add_parser("review"); review.add_argument("path")
    explain_delegation_command = sub.add_parser("explain-delegation"); explain_delegation_command.add_argument("--risk", choices=("auto", "simple", "medium", "complex", "high"), default="auto"); explain_delegation_command.add_argument("task")
    explain_route = sub.add_parser("explain-route"); explain_route.add_argument("--prefer-local", action="store_true"); explain_route.add_argument("task")
    delegate_fast = sub.add_parser("delegate-fast"); delegate_fast.add_argument("--max-seconds", type=int, default=60); delegate_fast.add_argument("task")
    local = sub.add_parser("local"); local_sub = local.add_subparsers(dest="local_action", required=True)
    for action in ("start", "stop", "restart", "repair", "status", "models", "smoke"):
        item = local_sub.add_parser(action)
        if action == "smoke": item.add_argument("task", nargs="?", default="只回复 LOCAL_DIRECT_OK")
    local_profiles = local_sub.add_parser("profiles")
    local_explain = local_sub.add_parser("explain-select"); local_explain.add_argument("task"); local_explain.add_argument("--risk", choices=("auto", "simple", "medium", "complex", "high"), default="auto"); local_explain.add_argument("--mode", choices=("auto", "local", "read", "review", "plan", "roleplay", "code"), default="auto")
    local_auto = local_sub.add_parser("auto-smoke"); local_auto.add_argument("--task", required=True); local_auto.add_argument("--risk", choices=("auto", "simple", "medium", "complex", "high"), default="auto"); local_auto.add_argument("--mode", choices=("auto", "local", "read", "review", "plan", "roleplay", "code"), default="auto")
    local_policy = local_sub.add_parser("policy"); local_policy.add_argument("--disable-model", action="append", default=[]); local_policy.add_argument("--enable-model", action="append", default=[]); local_policy.add_argument("--preferred"); local_policy.add_argument("--only"); local_policy.add_argument("--allow-slow-local", action="store_true"); local_policy.add_argument("--allow-bf16-auto", action="store_true"); local_policy.add_argument("--no-auto", action="store_true")
    local_select = local_sub.add_parser("select"); local_select.add_argument("model")
    local_configure = local_sub.add_parser("configure"); local_configure.add_argument("--llama-server-path"); local_configure.add_argument("--model-dir", action="append"); local_configure.add_argument("--port", type=int); local_configure.add_argument("--ctx-size", type=int); local_configure.add_argument("--timeout-seconds", type=int)
    serve = sub.add_parser("serve"); serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=18789); serve.add_argument("--gguf-dir", action="append", default=[]); serve.add_argument("--managed-gguf", help="optional discovered GGUF id to start persistently")
    handoff = sub.add_parser("handoff"); handoff.add_argument("task"); handoff.add_argument("--tests", default="NOT_RUN"); handoff.add_argument("--blockers", default="NONE"); handoff.add_argument("--constraints", default="")
    codex = sub.add_parser("codex-provider"); codex.add_argument("action", choices=("install", "switch-status")); codex.add_argument("--port", type=int, default=18789)
    config = sub.add_parser("config"); config.add_argument("action", choices=("show",))
    tools_policy = sub.add_parser("tools-policy"); tools_policy.add_argument("action", choices=("show", "set")); tools_policy.add_argument("policy", nargs="?", choices=VALID_POLICIES)
    deepseek = sub.add_parser("deepseek")
    deepseek_sub = deepseek.add_subparsers(dest="deepseek_action", required=True)
    deepseek_sub.add_parser("mode-probe")
    explain_mode = deepseek_sub.add_parser("explain-mode"); explain_mode.add_argument("task"); explain_mode.add_argument("--preference", choices=("auto", "normal", "search", "thinking", "expert"), default="auto"); explain_mode.add_argument("--model-alias"); explain_mode.add_argument("--codex-mode", default="CUSTOM_DEEPSEEK_TEXT_ONLY"); explain_mode.add_argument("--tools-policy", default="strict_reject"); explain_mode.add_argument("--no-search", action="store_true"); explain_mode.add_argument("--no-thinking", action="store_true"); explain_mode.add_argument("--no-expert", action="store_true")
    agent = sub.add_parser("agent", help="confirmation-gated Xiaoyu Local Agent")
    agent_sub = agent.add_subparsers(dest="agent_action", required=True)
    agent_plan = agent_sub.add_parser("plan"); agent_plan.add_argument("--task", required=True); agent_plan.add_argument("--brain-provider", choices=BRAIN_PROVIDERS, default="local-light"); agent_plan.add_argument("--risk", choices=("auto", "low", "medium", "high"), default="auto"); agent_plan.add_argument("--invoke-brain", action="store_true", help="explicitly invoke the selected advisory brain; never enabled by default")
    agent_readonly = agent_sub.add_parser("readonly"); agent_readonly.add_argument("--task", default=""); agent_readonly.add_argument("--plan")
    agent_draft = agent_sub.add_parser("draft-patch"); agent_draft.add_argument("--plan", required=True); agent_draft.add_argument("--confirm", action="store_true"); agent_draft.add_argument("--patch-text", default="")
    agent_apply = agent_sub.add_parser("apply"); agent_apply.add_argument("--plan", required=True); agent_apply.add_argument("--patch-file", required=True); agent_apply.add_argument("--confirm", action="store_true")
    agent_test = agent_sub.add_parser("test"); agent_test.add_argument("--plan", required=True); agent_test.add_argument("--test", choices=tuple(LocalAgent.SAFE_TESTS), default="python-unittest"); agent_test.add_argument("--confirm", action="store_true")
    agent_commit = agent_sub.add_parser("commit"); agent_commit.add_argument("--plan", required=True); agent_commit.add_argument("--file", action="append", default=[]); agent_commit.add_argument("--message", required=True); agent_commit.add_argument("--confirm", action="store_true")
    agent_stop = agent_sub.add_parser("stop"); agent_stop.add_argument("--plan")
    agent_api_health = agent_sub.add_parser("api-health")
    agent_api_plan = agent_sub.add_parser("api-plan"); agent_api_plan.add_argument("--task", required=True); agent_api_plan.add_argument("--brain-provider", choices=BRAIN_PROVIDERS, default="local-light"); agent_api_plan.add_argument("--risk", choices=("auto", "low", "medium", "high"), default="auto"); agent_api_plan.add_argument("--invoke-brain", action="store_true")
    agent_api_readonly = agent_sub.add_parser("api-readonly"); agent_api_readonly.add_argument("--task", default=""); agent_api_readonly.add_argument("--plan-id")
    agent_api_draft = agent_sub.add_parser("api-draft-patch"); agent_api_draft.add_argument("--plan-id", required=True); agent_api_draft.add_argument("--patch-text", default=""); agent_api_draft.add_argument("--confirm", action="store_true"); agent_api_draft.add_argument("--confirm-token")
    agent_api_apply = agent_sub.add_parser("api-apply"); agent_api_apply.add_argument("--plan-id", required=True); agent_api_apply.add_argument("--patch-file", required=True); agent_api_apply.add_argument("--confirm", action="store_true"); agent_api_apply.add_argument("--confirm-token")
    agent_api_test = agent_sub.add_parser("api-test"); agent_api_test.add_argument("--plan-id", required=True); agent_api_test.add_argument("--test", choices=tuple(LocalAgent.SAFE_TESTS), default="python-unittest"); agent_api_test.add_argument("--confirm", action="store_true"); agent_api_test.add_argument("--confirm-token")
    agent_api_commit = agent_sub.add_parser("api-commit"); agent_api_commit.add_argument("--plan-id", required=True); agent_api_commit.add_argument("--file", action="append", default=[]); agent_api_commit.add_argument("--message", required=True); agent_api_commit.add_argument("--confirm", action="store_true"); agent_api_commit.add_argument("--confirm-token")
    provider = sub.add_parser("provider"); provider_sub = provider.add_subparsers(dest="provider_action", required=True)
    provider_sub.add_parser("list")
    provider_add = provider_sub.add_parser("add"); provider_add.add_argument("id", nargs="?"); provider_add.add_argument("--display-name"); provider_add.add_argument("--type"); provider_add.add_argument("--base-url", required=True); provider_add.add_argument("--wire-api"); provider_add.add_argument("--api-key-env", default=""); provider_add.add_argument("--priority", type=int, default=100); provider_add.add_argument("--advanced", action="store_true")
    provider_show = provider_sub.add_parser("show"); provider_show.add_argument("id")
    for action in ("enable", "disable", "remove", "migrate-groq"):
        item = provider_sub.add_parser(action); item.add_argument("id")
    provider_preferred = provider_sub.add_parser("set-runtime-model"); provider_preferred.add_argument("id"); provider_preferred.add_argument("model", nargs="?")
    provider_deny = provider_sub.add_parser("set-model-denied"); provider_deny.add_argument("id"); provider_deny.add_argument("model"); provider_deny.add_argument("--allow", action="store_true")
    provider_clear = provider_sub.add_parser("clear-cooldown"); provider_clear.add_argument("id"); provider_clear.add_argument("model")
    provider_batch = provider_sub.add_parser("batch-model-policy"); provider_batch.add_argument("id"); provider_batch.add_argument("action", choices=("deny","allow","clear_deny","priority","only","reset")); provider_batch.add_argument("models", nargs="*"); provider_batch.add_argument("--priority", type=int)
    explain = provider_sub.add_parser("explain-selection"); explain.add_argument("virtual_model")
    provider_models = provider_sub.add_parser("models"); provider_models.add_argument("id")
    provider_refresh = provider_sub.add_parser("refresh-models"); provider_refresh.add_argument("id")
    provider_probe = provider_sub.add_parser("probe-runtime"); provider_probe.add_argument("id"); provider_probe.add_argument("--all", action="store_true")
    provider_diagnose = provider_sub.add_parser("diagnose"); provider_diagnose.add_argument("id")
    provider_live_smoke = provider_sub.add_parser("live-smoke"); provider_live_smoke.add_argument("id"); provider_live_smoke.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    task_policy = SelectionPolicy.from_values(getattr(args, "allow_provider", ()), getattr(args, "deny_provider", ()), getattr(args, "allow_model", ()), getattr(args, "deny_model", ()), getattr(args, "no_api", False), getattr(args, "no_local", False))
    config_data = load_config(args.config) if args.config else {}
    policy = SelectionPolicy.from_config(config_data).merged_with(task_policy) if args.config else task_policy
    router = Router.default(Path.cwd(), policy, FastLocalPolicy.from_config(config_data))
    if args.command == "tools-policy":
        if args.action == "show":
            emit({"codex_tools_policy": load_policy(), "default": "strict_reject", "path": str(policy_path())})
        else:
            if not args.policy: raise SystemExit("POLICY_REQUIRED")
            emit(save_policy(args.policy))
    elif args.command == "deepseek":
        if args.deepseek_action == "mode-probe":
            emit(probe_deepseek_modes())
        else:
            state = load_probe_state()
            emit(select_deepseek_mode(args.task, codex_mode=args.codex_mode, tools_policy=args.tools_policy, user_preference=args.preference, explicit_model_alias=args.model_alias, availability=state.get("modes"), search_allowed=not args.no_search, thinking_allowed=not args.no_thinking, expert_allowed=not args.no_expert))
    elif args.command == "agent":
        if args.agent_action == "api-health":
            emit(agent_api_request("/agent/health"))
            return
        if args.agent_action == "api-plan":
            emit(agent_api_request("/agent/plan", {"task": args.task, "brain_provider": args.brain_provider, "risk": args.risk, "invoke_brain": args.invoke_brain}))
            return
        if args.agent_action == "api-readonly":
            emit(agent_api_request("/agent/readonly", {"task": args.task, "plan_id": args.plan_id} if args.plan_id else {"task": args.task}))
            return
        if args.agent_action in {"api-draft-patch", "api-apply", "api-test", "api-commit"}:
            payload = {"plan_id": args.plan_id, "confirm": args.confirm}
            if args.confirm_token:
                payload["confirm_token"] = args.confirm_token
            if args.agent_action == "api-draft-patch":
                payload.update({"patch_text": args.patch_text})
                emit(agent_api_request("/agent/draft-patch", payload))
            elif args.agent_action == "api-apply":
                payload.update({"patch_file": args.patch_file})
                emit(agent_api_request("/agent/apply", payload))
            elif args.agent_action == "api-test":
                payload.update({"test": args.test})
                emit(agent_api_request("/agent/test", payload))
            else:
                payload.update({"files": args.file, "message": args.message})
                emit(agent_api_request("/agent/commit", payload))
            return
        agent_runner = LocalAgent(Path.cwd())
        try:
            if args.agent_action == "plan":
                emit(agent_runner.plan(args.task, brain_provider=args.brain_provider, risk=args.risk, invoke_brain_now=args.invoke_brain))
            elif args.agent_action == "readonly":
                emit(agent_runner.readonly(args.task, args.plan))
            elif args.agent_action == "draft-patch":
                emit(agent_runner.draft_patch(args.plan, confirm=args.confirm, patch_text=args.patch_text))
            elif args.agent_action == "apply":
                emit(agent_runner.apply(args.plan, args.patch_file, confirm=args.confirm))
            elif args.agent_action == "test":
                emit(agent_runner.test(args.plan, test_name=args.test, confirm=args.confirm))
            elif args.agent_action == "commit":
                emit(agent_runner.commit(args.plan, args.file, args.message, confirm=args.confirm))
            else:
                emit(agent_runner.stop(args.plan))
        except LocalAgentError as exc:
            emit({"status": "DENIED", "error_code": exc.code, "codex_agent_used": "NO", "auto_file_modify": "NO", "auto_command_execute": "NO", "auto_commit": "NO", "auto_push": "NO", "auto_deploy": "NO", "secrets_logged": "NO"})
    elif args.command == "doctor": emit(router.doctor())
    elif args.command == "status": emit(router.status())
    elif args.command == "models":
        ld = router.local.models(); ad = router.api.models(); _, le = router.provider_models("local"); _, ae = router.provider_models("api")
        emit({"DISCOVERED_MODELS": {"local": ld, "api": ad}, "ALLOWED_MODELS": {"local": le, "api": ae}, "ELIGIBLE_MODELS": {"local": le, "api": ae}, "MODEL_DISCOVERY_SUPPORTED": {"local": "YES", "api": router.api.model_discovery_supported}})
    elif args.command == "classify": emit(router.route(args.task))
    elif args.command == "explain-route":
        direct = router.local.managed
        status = direct.status()
        if status["server_running"] == "YES": reason = "direct local server is running and preferred"
        elif status["llama_server_found"] == "YES" and status["selected_model"]: reason = "direct local is configured and will auto-start on request"
        elif status["llama_server_found"] == "NO": reason = "llama-server was not found"
        elif status["model_count"] == 0: reason = "no configured GGUF model was discovered"
        else: reason = "a GGUF model must be selected before direct local can start"
        emit({"direct_local_configured": status["configured"], "llama_server_found": status["llama_server_found"], "selected_gguf_model": status["selected_model"], "server_running": status["server_running"], "why_selected": reason if args.prefer_local else "AUTO policy decides by risk and task size", "skipped_reason": None if args.prefer_local and status["selected_model"] else reason, "fallback_provider": "lmstudio"})
    elif args.command == "ask":
        found, eligible = router.provider_models(args.provider)
        selected = router.selection_policy.choose(args.provider, found, "coder", args.local_model if args.provider == "local" else args.api_model)
        if not selected: raise SystemExit("NO_ELIGIBLE_MODEL")
        provider = router.local if args.provider == "local" else router.api; provider.model = selected
        emit({"provider": args.provider, "response": provider.ask(args.task)})
    elif args.command == "provider":
        if args.provider_action == "list":
            data = provider_config.load(); emit({"providers": [{"display_name": value.get("display_name", key), "id": key, "type": value.get("type"), "enabled": value.get("enabled", True), "configured": bool(value.get("base_url") and (value.get("type") == "lmstudio" or value.get("api_key_env")))} for key, value in data["providers"].items()]})
        elif args.provider_action == "add":
            existing = set(provider_config.load()["providers"])
            provider_id = args.id or suggested_provider_id(args.base_url, existing)
            provider_type = args.type or suggested_provider_type(args.base_url)
            wire_api = args.wire_api or ("responses" if provider_type == "openai_compatible" else "chat_completions")
            emit(provider_config.upsert(provider_id, {"display_name": args.display_name or default_display_name(provider_id), "type": provider_type, "base_url": args.base_url, "wire_api": wire_api, "api_key_env": args.api_key_env, "enabled": True, "model_discovery": True, "models": [], "priority": args.priority, "setup_mode": "advanced" if args.advanced else "automatic"}))
        elif args.provider_action == "show": emit(provider_config.load()["providers"].get(args.id) or (_ for _ in ()).throw(KeyError("provider not found")))
        elif args.provider_action in {"enable", "disable"}: emit(provider_config.set_enabled(args.id, args.provider_action == "enable"))
        elif args.provider_action == "remove": provider_config.remove(args.id); emit({"status": "REMOVED", "id": args.id})
        elif args.provider_action == "migrate-groq":
            entry = provider_config.load()["providers"].get(args.id)
            if not entry: raise KeyError("provider not found")
            if "api.groq.com/openai/v1" not in str(entry.get("base_url", "")).rstrip("/").lower(): raise ValueError("NOT_GROQ_BASE_URL")
            emit(provider_config.migrate_to_groq(args.id))
        elif args.provider_action == "diagnose":
            entry = provider_config.load()["providers"].get(args.id)
            if not entry: raise KeyError("provider not found")
            if entry.get("type") != "groq": raise ValueError("PROVIDER_DIAGNOSE_SUPPORTED_FOR_GROQ_ONLY")
            emit(diagnose_groq(args.id, entry))
        elif args.provider_action == "live-smoke":
            entry = provider_config.load()["providers"].get(args.id)
            if not entry: raise KeyError("provider not found")
            if entry.get("type") != "groq": raise ValueError("PROVIDER_LIVE_SMOKE_SUPPORTED_FOR_GROQ_ONLY")
            emit(live_smoke_groq(args.id, entry, args.confirm))
        elif args.provider_action == "set-runtime-model": emit(provider_config.set_runtime_model_preference(args.id, args.model))
        elif args.provider_action == "set-model-denied": emit(provider_config.set_model_denied(args.id, args.model, not args.allow))
        elif args.provider_action == "clear-cooldown": RuntimeModelState().clear_cooldown(args.id, args.model); emit({"status": "COOLDOWN_CLEARED", "id": args.id, "model": args.model})
        elif args.provider_action == "batch-model-policy": emit(provider_config.batch_model_policy(args.id, args.models, args.action, args.priority))
        elif args.provider_action == "explain-selection": emit(RouterService().explain_selection(args.virtual_model))
        else:
            if args.id == "local": provider = router.local
            else:
                entry = provider_config.load()["providers"].get(args.id)
                if not entry: raise KeyError("provider not found")
                if entry.get("type") == "lmstudio": provider = LMStudioProvider(entry.get("base_url", "http://127.0.0.1:1234/v1"))
                elif entry.get("type") == "groq": provider = GroqProvider(key_env=entry.get("api_key_env", "GROQ_API_KEY"), provider_id=args.id, timeout=int(entry.get("request_timeout", 20)))
                else:
                    profile_auth = codex_profile_requires_bearer_auth(entry.get("base_url", ""))
                    provider = OpenAICompatibleProvider(entry.get("base_url"), key_env=entry.get("api_key_env", ""), wire_api=entry.get("wire_api", "chat_completions"), requires_bearer_auth=entry.get("requires_bearer_auth", profile_auth), header_env=entry.get("headers", {}), provider_id=args.id, provider_metadata=entry, model_env=entry.get("model_env"))
            if args.provider_action == "probe-runtime":
                records = provider.discover_models(); state = RuntimeModelState()
                states = model_state_report(args.id, entry if args.id != "local" else {}, records, policy, state)
                candidates, excluded = text_candidates(states["ALLOWED_MODELS"]); results = []
                for model in candidates:
                    result = probe_model(provider, model, state); results.append(result)
                    if result["status"] == "PASS" and not args.all: break
                states = model_state_report(args.id, entry if args.id != "local" else {}, records, policy, state)
                if args.id != "local": provider_config.record_model_registry(args.id, states, getattr(provider, "remote_model_list_status", "UNKNOWN"))
                emit({"provider": args.id, "text_candidates": candidates, "excluded_models": excluded, "probe_results": results, **states})
            else:
                records = provider.refresh_models() if args.provider_action == "refresh-models" else provider.discover_models()
                states = model_state_report(args.id, entry if args.id != "local" else {}, records, policy, RuntimeModelState())
                if args.provider_action == "refresh-models" and args.id != "local":
                    provider_config.record_model_registry(args.id, states, getattr(provider, "remote_model_list_status", "UNKNOWN"))
                payload = {"provider": args.id, "MODEL_DISCOVERY_SUPPORTED": getattr(provider, "model_discovery_supported", "YES"), "REMOTE_MODEL_LIST_STATUS": getattr(provider, "remote_model_list_status", "NOT_APPLICABLE"), "models": [{"id": record.model_id, "owned_by": record.owned_by, "availability": record.availability, "source": (record.raw_metadata or {}).get("source"), "validation": (record.raw_metadata or {}).get("validation")} for record in records], **states}
                if args.id != "local": payload["diagnostic"] = provider_diagnostic(args.id, entry, payload["REMOTE_MODEL_LIST_STATUS"])
                emit(payload)
    elif args.command in {"delegate", "auto"}: emit(router.delegate(args.task, Mode(args.mode) if args.command == "delegate" and args.mode else None, getattr(args, "api_model", None), getattr(args, "local_model", None)))
    elif args.command == "review": emit(router.delegate("Review path: " + args.path))
    elif args.command == "local":
        backend = router.local.managed
        try:
            if args.local_action == "configure":
                local_data = load_local_backend_config()
                if args.llama_server_path is not None: local_data["llama_server_path"] = args.llama_server_path
                if args.model_dir: local_data["model_dirs"] = list(dict.fromkeys([*local_data.get("model_dirs", []), *args.model_dir]))
                if args.port is not None: local_data["port"] = args.port; backend.port = args.port
                if args.ctx_size is not None: local_data["ctx_size"] = args.ctx_size
                if args.timeout_seconds is not None: local_data["timeout_seconds"] = args.timeout_seconds
                emit({"status": "CONFIGURED", "config_path": str(save_local_backend_config(local_data))})
            elif args.local_action == "status": emit(backend.status())
            elif args.local_action == "models": emit({"models": [model.as_dict() for model in backend.discover()], "llama_server_found": "YES" if backend.executable_available() else "NO", "source": "lmstudio_gguf_direct", "LMSTUDIO_GGUF_REUSE": "YES", "NO_FULL_DISK_SCAN": "YES"})
            elif args.local_action == "profiles":
                profiles = backend.profiles(); emit({"profiles": profiles, "model_count": len(profiles), "source": "gguf_filename_profile"})
            elif args.local_action == "explain-select": emit(backend.explain_select(args.task, risk=args.risk, mode=args.mode))
            elif args.local_action == "auto-smoke": emit(backend.auto_smoke(args.task, risk=args.risk, mode=args.mode))
            elif args.local_action == "policy":
                local_data = load_local_backend_config()
                disabled = set(str(item) for item in local_data.get("manual_disabled_models", []) if item)
                disabled.update(args.disable_model); disabled.difference_update(args.enable_model)
                local_data["manual_disabled_models"] = sorted(disabled)
                if args.preferred is not None: local_data["manual_preferred_model"] = args.preferred
                if args.only is not None: local_data["manual_only_model"] = args.only
                if args.allow_slow_local: local_data["allow_slow_local"] = True
                if args.allow_bf16_auto: local_data["allow_bf16_auto"] = True
                if args.no_auto: local_data["auto_select_model"] = False
                emit({"status": "POLICY_SAVED", "config_path": str(save_local_backend_config(local_data)), "auto_select_model": local_data.get("auto_select_model", True), "manual_disabled_models": local_data.get("manual_disabled_models", []), "manual_preferred_model": local_data.get("manual_preferred_model", ""), "manual_only_model": local_data.get("manual_only_model", ""), "allow_slow_local": local_data.get("allow_slow_local", False), "allow_bf16_auto": local_data.get("allow_bf16_auto", False)})
            elif args.local_action == "select": emit({"status": "SELECTED", "model": backend.select(args.model).as_dict()})
            elif args.local_action == "start": emit(backend.start())
            elif args.local_action == "stop": emit(backend.stop())
            elif args.local_action == "restart": emit(backend.restart())
            elif args.local_action == "repair": emit(backend.repair())
            elif args.local_action == "smoke": emit({"status": "PASS", "response": backend.ask(args.task), "model": backend.selected.model_id if backend.selected else None, "endpoint": backend.endpoint()})
        except Exception as exc:
            code = str(exc) or "LOCAL_DIRECT_BACKEND_ERROR"
            emit({"status": "ERROR", "error_code": code})
    elif args.command == "explain-delegation": emit({**explain_delegation(args.task, risk_override=args.risk), **RouterService().explain_fast_delegation()})
    elif args.command == "delegate-fast": emit(RouterService().delegate_fast_readonly(args.task, max_seconds=max(1, min(args.max_seconds, 300))))
    elif args.command == "serve":
        mode = NetworkMode.OFFLINE if args.offline else NetworkMode.AUTO
        configured_dirs = config_data.get("local", {}).get("model_directories", []) if isinstance(config_data.get("local", {}), dict) else []
        all_dirs = [Path(path) for path in [*configured_dirs, *args.gguf_dir]]
        managed = ManagedLlamaCppBackend(all_dirs if all_dirs else None)
        if args.managed_gguf:
            selected = next((model for model in managed.discover() if model.model_id == args.managed_gguf), None)
            if not selected: raise SystemExit("MANAGED_GGUF_NOT_FOUND")
            managed.start(selected)
        server = RouterResponsesServer(RouterService(mode, local=LocalBackend(managed=managed), fast_local_policy=FastLocalPolicy.from_config(config_data)), args.host, args.port)
        print(json.dumps({"ROUTER_RESPONSES_SERVER": "RUNNING", "ROUTER_LISTEN_ADDRESS": f"http://{args.host}:{args.port}/v1", "AGENT_API_ENDPOINT": AGENT_API_BASE if args.host in {"127.0.0.1", "localhost", "::1"} and args.port == 18789 else "DISABLED_FOR_NON_DEFAULT_PORT", "LOCALHOST_ONLY": "YES", "NETWORK_MODE": mode.value}))
        server.serve_forever()
    elif args.command == "handoff": emit(compact_handoff(Path.cwd(), args.task, args.tests, args.blockers, args.constraints))
    elif args.command == "codex-provider":
        emit(install_xiaoyu_router_provider(port=args.port) if args.action == "install" else {"SAME_THREAD_PROVIDER_SWITCH": same_thread_provider_switch_support()})
    else: emit({"config_example": str(Path(__file__).parents[2] / "config" / "config.example.yaml"), "api_key_env": "XIAOYU_CODER_API_KEY"})


if __name__ == "__main__": main()
