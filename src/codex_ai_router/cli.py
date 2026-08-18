from __future__ import annotations

import argparse
import json
from pathlib import Path
from .router import Router
from .task import Mode
from .model_policy import SelectionPolicy
from .configuration import load_config


def emit(data): print(json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, dict) else data.to_json())


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
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status", "models"):
        sub.add_parser(name)
    classify = sub.add_parser("classify"); classify.add_argument("task")
    ask = sub.add_parser("ask"); ask.add_argument("--provider", choices=("local", "api"), required=True); add_policy_args(ask); ask.add_argument("task")
    delegate = sub.add_parser("delegate"); delegate.add_argument("--mode", choices=[m.value for m in Mode]); add_policy_args(delegate); delegate.add_argument("task")
    auto = sub.add_parser("auto"); add_policy_args(auto); auto.add_argument("task")
    review = sub.add_parser("review"); review.add_argument("path")
    config = sub.add_parser("config"); config.add_argument("action", choices=("show",))
    provider = sub.add_parser("provider"); provider_sub = provider.add_subparsers(dest="provider_action", required=True)
    provider_models = provider_sub.add_parser("models"); provider_models.add_argument("provider", choices=("api", "local"))
    provider_refresh = provider_sub.add_parser("refresh-models"); provider_refresh.add_argument("provider", choices=("api", "local"))
    args = parser.parse_args()
    task_policy = SelectionPolicy.from_values(getattr(args, "allow_provider", ()), getattr(args, "deny_provider", ()), getattr(args, "allow_model", ()), getattr(args, "deny_model", ()), getattr(args, "no_api", False), getattr(args, "no_local", False))
    policy = SelectionPolicy.from_config(load_config(args.config)).merged_with(task_policy) if args.config else task_policy
    router = Router.default(Path.cwd(), policy)
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
        provider = router.local if args.provider == "local" else router.api
        records = provider.refresh_models() if args.provider_action == "refresh-models" else provider.discover_models()
        emit({"provider": args.provider, "MODEL_DISCOVERY_SUPPORTED": getattr(provider, "model_discovery_supported", "YES"), "models": [{"id": record.model_id, "owned_by": record.owned_by, "availability": record.availability} for record in records]})
    elif args.command in {"delegate", "auto"}: emit(router.delegate(args.task, Mode(args.mode) if args.command == "delegate" and args.mode else None, getattr(args, "api_model", None), getattr(args, "local_model", None)))
    elif args.command == "review": emit(router.delegate("Review path: " + args.path))
    else: emit({"config_example": str(Path(__file__).parents[2] / "config" / "config.example.yaml"), "api_key_env": "XIAOYU_CODER_API_KEY"})


if __name__ == "__main__": main()
