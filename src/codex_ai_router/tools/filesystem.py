from pathlib import Path
from ..security.permissions import canonical_inside
from ..security.secrets import is_sensitive_path


def read_file(root: Path, path: Path) -> str:
    target = canonical_inside(root, path)
    if is_sensitive_path(target):
        raise PermissionError("sensitive file denied")
    return target.read_text(encoding="utf-8")


def write_file(root: Path, path: Path, content: str) -> None:
    target = canonical_inside(root, path)
    if is_sensitive_path(target):
        raise PermissionError("sensitive file denied")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
