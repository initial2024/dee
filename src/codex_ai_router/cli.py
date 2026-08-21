from __future__ import annotations

import argparse
import json
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
from .providers.local_backend import LocalBackend, ManagedLlamaCppBackend
from .providers.runtime_models import RuntimeModelState, probe_model, text_candidates
from .providers.model_states import model_state_report
from .handoff import compact_handoff
from .codex_integration import install_xiaoyu_router_provider, same_thread_provider_switch_support


def emit(data): print(json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, dict) else data.to_json())


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
    serve = sub.add_parser("serve"); serve.add_argument("--host", default="127.0.0.1"); serve.add_argument("--port", type=int, default=18789); serve.add_argument("--gguf-dir", action="append", default=[]); serve.add_argument("--managed-gguf", help="optional discovered GGUF id to start persistently")
    handoff = sub.add_parser("handoff"); handoff.add_argument("task"); handoff.add_argument("--tests", default="NOT_RUN"); handoff.add_argument("--blockers", default="NONE"); handoff.add_argument("--constraints", default="")
    codex = sub.add_parser("codex-provider"); codex.add_argument("action", choices=("install", "switch-status")); codex.add_argument("--port", type=int, default=18789)
    config = sub.add_parser("config"); config.add_argument("action", choices=("show",))
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
    args = parser.parse_args()
    task_policy = SelectionPolicy.from_values(getattr(args, "allow_provider", ()), getattr(args, "deny_provider", ()), getattr(args, "allow_model", ()), getattr(args, "deny_model", ()), getattr(args, "no_api", False), getattr(args, "no_local", False))
    config_data = load_config(args.config) if args.config else {}
    policy = SelectionPolicy.from_config(config_data).merged_with(task_policy) if args.config else task_policy
    router = Router.default(Path.cwd(), policy, FastLocalPolicy.from_config(config_data))
    if args.command == "doctor": emit(router.doctor())
    elif args.command == "status": emit(router.status())
    elif args.command == "models":
        ld = router.local.models(); ad = router.api.models(); _, le = router.provider_models("local"); _, ae = router.provider_models("api")
        emit({"DISCOVERED_MODELS": {"local": ld, "api": ad}, "ALLOWED_MODELS": {"local": le, "api": ae}, "ELIGIBLE_MODELS": {"local": le, "api": ae}, "MODEL_DISCOVERY_SUPPORTED": {"local": "YES", "api": router.api.model_discovery_supported}})
    elif args.command == "classify": emit(router.route(args.task))
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
    elif args.command == "serve":
        mode = NetworkMode.OFFLINE if args.offline else NetworkMode.AUTO
        configured_dirs = config_data.get("local", {}).get("model_directories", []) if isinstance(config_data.get("local", {}), dict) else []
        managed = ManagedLlamaCppBackend([Path(path) for path in [*configured_dirs, *args.gguf_dir]])
        if args.managed_gguf:
            selected = next((model for model in managed.discover() if model.model_id == args.managed_gguf), None)
            if not selected: raise SystemExit("MANAGED_GGUF_NOT_FOUND")
            managed.start(selected)
        server = RouterResponsesServer(RouterService(mode, local=LocalBackend(managed=managed), fast_local_policy=FastLocalPolicy.from_config(config_data)), args.host, args.port)
        print(json.dumps({"ROUTER_RESPONSES_SERVER": "RUNNING", "ROUTER_LISTEN_ADDRESS": f"http://{args.host}:{args.port}/v1", "LOCALHOST_ONLY": "YES", "NETWORK_MODE": mode.value}))
        server.serve_forever()
    elif args.command == "handoff": emit(compact_handoff(Path.cwd(), args.task, args.tests, args.blockers, args.constraints))
    elif args.command == "codex-provider":
        emit(install_xiaoyu_router_provider(port=args.port) if args.action == "install" else {"SAME_THREAD_PROVIDER_SWITCH": same_thread_provider_switch_support()})
    else: emit({"config_example": str(Path(__file__).parents[2] / "config" / "config.example.yaml"), "api_key_env": "XIAOYU_CODER_API_KEY"})


if __name__ == "__main__": main()
