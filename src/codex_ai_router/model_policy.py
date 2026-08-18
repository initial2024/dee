from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderPolicy:
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()

    def permits(self, provider: str) -> bool:
        return provider not in self.deny and (not self.allow or provider in self.allow)


@dataclass(frozen=True)
class ModelPolicy:
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()

    def permits(self, provider: str, model: str) -> bool:
        key = f"{provider}:{model}"
        return key not in self.deny and (not self.allow or key in self.allow)


@dataclass(frozen=True)
class SelectionPolicy:
    providers: ProviderPolicy = field(default_factory=ProviderPolicy)
    models: ModelPolicy = field(default_factory=ModelPolicy)
    roles: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def eligible(self, provider: str, discovered: list[str]) -> list[str]:
        if not self.providers.permits(provider):
            return []
        return [model for model in discovered if self.models.permits(provider, model)]

    def choose(self, provider: str, discovered: list[str], role: str, override: str | None = None) -> str | None:
        eligible = self.eligible(provider, discovered)
        # An explicit task model selection is strict: it cannot bypass policy or silently switch models.
        if override:
            return override if override in eligible else None
        preferred = self.roles.get(role, ())
        for item in preferred:
            prefix, _, model = item.partition(":")
            if prefix == provider and model in eligible:
                return model
        return eligible[0] if eligible else None

    @classmethod
    def from_values(cls, allow_provider=(), deny_provider=(), allow_model=(), deny_model=(), no_api=False, no_local=False, roles=None):
        deny_provider = set(deny_provider)
        if no_api: deny_provider.add("api")
        if no_local: deny_provider.add("local")
        return cls(ProviderPolicy(frozenset(allow_provider), frozenset(deny_provider)), ModelPolicy(frozenset(allow_model), frozenset(deny_model)), roles or {})

    @classmethod
    def from_config(cls, config: dict) -> "SelectionPolicy":
        providers = config.get("provider_policy", {})
        models = config.get("models", {})
        roles = {name: tuple(value.get("preferred", ())) for name, value in config.get("roles", {}).items() if isinstance(value, dict)}
        return cls.from_values(providers.get("allow", ()), providers.get("deny", ()), models.get("allow", ()), models.get("deny", ()), roles=roles)

    def merged_with(self, task: "SelectionPolicy") -> "SelectionPolicy":
        def merge_allow(left, right): return left & right if left and right else (left or right)
        return SelectionPolicy(ProviderPolicy(merge_allow(self.providers.allow, task.providers.allow), self.providers.deny | task.providers.deny), ModelPolicy(merge_allow(self.models.allow, task.models.allow), self.models.deny | task.models.deny), {**self.roles, **task.roles})
