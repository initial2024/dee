"""Bounded, read-only project context collection for Xiaoyu coordination."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import socket
import subprocess
from typing import Any, Iterable

MAX_SNIPPETS = 8
MAX_SNIPPET_CHARS = 1800
MAX_FILE_SEARCH = 40
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".codex", ".agents"}
SENSITIVE_NAMES = re.compile(r"(?i)(\.env|secret|credential|password|token|cookie|storage(state)?|\.pem$|\.key$)")
SECRET_VALUE = re.compile(r"(?i)(authorization|bearer|cookie|token|api[_ -]?key|password|secret)\s*[:=]\s*[^\s,;]+")


def _redact(value: object, limit: int = 4000) -> str:
    text = str(value or "")
    text = SECRET_VALUE.sub(lambda m: m.group(1) + "=[REDACTED]", text)
    return text[:limit]


def _safe_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return not any(part in SKIP_DIRS for part in path.parts) and not SENSITIVE_NAMES.search(path.name)


def _run_readonly(command: tuple[str, ...], root: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(list(command), cwd=root, capture_output=True, text=True, timeout=8, shell=False, check=False)
        return {"command": " ".join(command), "exit_code": completed.returncode, "output": _redact(completed.stdout + completed.stderr)}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": " ".join(command), "exit_code": 1, "output": type(exc).__name__}


def _redact_git_output(value: str, root: Path) -> str:
    safe_lines: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        candidate = stripped[2:].strip() if len(stripped) > 2 and stripped[1].isspace() else stripped
        if candidate and not _safe_path(root / candidate, root):
            safe_lines.append("[SENSITIVE_PATH_REDACTED]")
        else:
            safe_lines.append(_redact(line, 500))
    return "\n".join(safe_lines)


def _keywords(task: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", task or "")
    return list(dict.fromkeys(word.lower() for word in words))[:12]


@dataclass
class ContextCollector:
    root: Path
    ledger: Path | None = None

    def __post_init__(self) -> None:
        self.root = self.root.resolve()

    def _changed_files(self) -> list[str]:
        result = _run_readonly(("git", "diff", "--name-only"), self.root)
        return [line.strip() for line in result["output"].splitlines() if line.strip() and len(line) < 300 and _safe_path(self.root / line.strip(), self.root)][:MAX_FILE_SEARCH]

    def _relevant_files(self, task: str, changed: Iterable[str]) -> list[str]:
        keywords = _keywords(task)
        candidates: list[tuple[int, str]] = []
        for raw in list(changed):
            path = self.root / raw
            if _safe_path(path, self.root):
                candidates.append((100, raw))
        try:
            for path in self.root.rglob("*"):
                if len(candidates) >= MAX_FILE_SEARCH or not path.is_file() or not _safe_path(path, self.root):
                    continue
                rel = path.relative_to(self.root).as_posix()
                score = sum(2 for keyword in keywords if keyword in rel.lower())
                if path.suffix.lower() in {".py", ".ps1", ".md", ".json", ".toml", ".js", ".ts", ".tsx"}:
                    score += 1
                if score:
                    candidates.append((score, rel))
        except OSError:
            pass
        return list(dict.fromkeys(item[1] for item in sorted(candidates, key=lambda item: (-item[0], item[1]))))[:MAX_FILE_SEARCH]

    def _snippets(self, files: Iterable[str]) -> list[dict[str, str]]:
        snippets: list[dict[str, str]] = []
        for rel in files:
            if len(snippets) >= MAX_SNIPPETS:
                break
            path = (self.root / rel).resolve()
            if not _safe_path(path, self.root):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            snippets.append({"path": rel, "content": _redact(text, MAX_SNIPPET_CHARS)})
        return snippets

    def _recent_records(self) -> list[dict[str, Any]]:
        target = self.ledger or (Path.home() / ".codex-ai-router" / "local-agent" / "ledger.jsonl")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()[-5:]
        except OSError:
            return []
        allowed = {"id", "created_at", "mode", "status", "error_code", "duration_ms", "brain_provider"}
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                records.append({key: value.get(key) for key in allowed if key in value})
        return records

    def _loopback_ports(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for port in (8791, 8792, 8793, 18789):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.15)
            try:
                result[str(port)] = "LISTENING" if sock.connect_ex(("127.0.0.1", port)) == 0 else "NOT_LISTENING"
            except OSError:
                result[str(port)] = "UNKNOWN"
            finally:
                sock.close()
        return result

    def collect(self, task: str, *, selected_brain: str = "auto") -> dict[str, Any]:
        changed = self._changed_files()
        relevant = self._relevant_files(task, changed)
        git_status = _run_readonly(("git", "status", "--short"), self.root)
        diff_stat = _run_readonly(("git", "diff", "--stat"), self.root)
        return {
            "task": _redact(task, 1200),
            "project_root": str(self.root),
            "git_status": _redact_git_output(git_status["output"], self.root),
            "changed_files": changed,
            "relevant_files": relevant,
            "file_snippets": self._snippets(relevant),
            "diff_summary": _redact_git_output(diff_stat["output"], self.root),
            "risk_flags": [],
            "redaction_applied": True,
            "collection_mode": "read_only",
            "selected_brain": selected_brain,
            "task_difficulty": "unknown",
            "recent_agent_metadata": self._recent_records(),
            "loopback_ports": self._loopback_ports(),
            "commands_executed": ["git status --short", "git diff --name-only", "git diff --stat"],
            "files_modified": "NO",
            "write_commands_executed": "NO",
            "tests_executed": "NO",
            "secrets_logged": "NO",
            "prompt_response_logged": "NO",
        }


__all__ = ["ContextCollector", "MAX_SNIPPETS", "MAX_SNIPPET_CHARS"]
