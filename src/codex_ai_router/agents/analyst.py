from ..security.secrets import redact


def analyze(provider, prompt: str, policy=None, provider_name: str = "local") -> str:
    if policy and not policy.providers.permits(provider_name):
        raise PermissionError("provider denied by policy")
    return redact(provider.ask("Provide a read-only concise analysis. " + prompt))
