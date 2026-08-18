from __future__ import annotations

from .task import Risk

HIGH = ("delete", "remove user", "migration", "deploy", "credential", "api key", "authentication", "authorization", "permission", "force push", "payment", "release signing", "security", "shell", "system configuration")
CRITICAL = ("production deploy", "database migration", "git history rewrite", "private key", "user data deletion")


def classify(prompt: str) -> tuple[str, Risk]:
    text = prompt.lower()
    if any(word in text for word in CRITICAL):
        return "HIGH_RISK", Risk.CRITICAL
    if any(word in text for word in HIGH):
        return "HIGH_RISK", Risk.HIGH
    if any(word in text for word in ("test", "pytest", "unit test", "coverage")):
        return "TEST_GENERATION", Risk.MEDIUM
    if any(word in text for word in ("refactor", "architecture", "multiple files", "large")):
        return "LARGE_REFACTOR", Risk.HIGH
    if any(word in text for word in ("log", "traceback", "error log")):
        return "LOG_ANALYSIS", Risk.LOW
    if any(word in text for word in ("compact context", "context compaction", "compress context")):
        return "CONTEXT_COMPACTION", Risk.LOW
    if any(word in text for word in ("small diff", "diff review", "review diff")):
        return "SMALL_REVIEW", Risk.LOW
    if any(word in text for word in ("translate", "translation")):
        return "TRANSLATION", Risk.LOW
    if any(word in text for word in ("readme", "documentation", "docs", "summarize")):
        return "DOCS", Risk.LOW
    if any(word in text for word in ("fix", "implement", "function", "bug")):
        return "SMALL_CODE", Risk.MEDIUM
    if any(word in text for word in ("read", "search", "inspect", "explain")):
        return "SIMPLE_READ", Risk.LOW
    return "UNKNOWN", Risk.MEDIUM
