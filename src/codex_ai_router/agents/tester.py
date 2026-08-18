from pathlib import Path
from ..tools.subprocess_runner import run_command


def targeted_test(root: Path) -> tuple[int, str]:
    if (root / "pytest.ini").exists() or (root / "tests").exists():
        return run_command(["python", "-m", "pytest", "-q"], root, timeout=120)
    return 0, "NO_TARGETED_TEST_DETECTED"
