from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_ai_router.patch_draft import (
    PatchSynthesizerError,
    extract_structured_patch,
    persist_structured_patch_draft,
    synthesize_structured_patch,
    validate_unified_diff,
)


class StructuredPatchSynthesizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "sample.py").write_text("first\nanchor\nlast\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _payload(path: str = "src/sample.py", find: str = "anchor\n", replace: str = "replacement\n") -> dict[str, object]:
        return {"patch_type": "structured_patch", "files": [{"path": path, "operations": [{"op": "replace_block", "find": find, "replace": replace, "reason": "test"}]}]}

    def test_valid_replace_block_yields_project_relative_unified_diff_and_metadata(self):
        diff, files, count = synthesize_structured_patch(self.root, self._payload())
        valid, detected = validate_unified_diff(diff)
        self.assertTrue(valid)
        self.assertEqual((files, detected, count), (["src/sample.py"], ["src/sample.py"], 1))
        self.assertIn("@@", diff)
        self.assertEqual((self.root / "src" / "sample.py").read_text(encoding="utf-8"), "first\nanchor\nlast\n")
        patch, metadata = persist_structured_patch_draft(self.root, diff, task_name="test", files_targeted=files, operations_count=count, risk_level="low")
        self.assertTrue(patch.is_file())
        body = json.loads(metadata.read_text(encoding="utf-8"))
        self.assertEqual(body["source"], "structured_patch")
        self.assertEqual(set(body), {"created_at", "task_name", "source", "files_targeted", "operations_count", "unified_diff_detected", "patch_draft_applied", "tests_executed", "commit_created", "risk_level", "redaction_applied"})

    def test_invalid_operations_are_precisely_rejected(self):
        cases = [
            (self._payload(path="/tmp/x.py"), "PATCH_PATH_INVALID"),
            (self._payload(path="../x.py"), "PATCH_PATH_TRAVERSAL"),
            (self._payload(path="missing.py"), "PATCH_TARGET_FILE_NOT_FOUND"),
            (self._payload(find="missing"), "PATCH_ANCHOR_NOT_FOUND"),
            (self._payload(find="\n"), "PATCH_ANCHOR_NOT_UNIQUE"),
            (self._payload(replace=""), "PATCH_DELETE_NOT_ALLOWED"),
            ({"patch_type": "structured_patch", "files": [{"path": "src/sample.py", "operations": []}]}, "PATCH_DRAFT_EMPTY"),
        ]
        for payload, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(PatchSynthesizerError, code):
                synthesize_structured_patch(self.root, payload)

    def test_only_replace_block_is_accepted_and_json_can_be_extracted(self):
        payload = self._payload()
        payload["files"][0]["operations"][0]["op"] = "delete"
        with self.assertRaisesRegex(PatchSynthesizerError, "PATCH_DRAFT_FORMAT_INVALID"):
            synthesize_structured_patch(self.root, payload)
        fenced = "analysis\n```json\n" + json.dumps(self._payload()) + "\n```"
        self.assertEqual(extract_structured_patch(fenced), self._payload())

    def test_empty_and_oversized_target_are_rejected_without_writing_source(self):
        with self.assertRaisesRegex(PatchSynthesizerError, "PATCH_DRAFT_EMPTY"):
            synthesize_structured_patch(self.root, self._payload(replace="anchor\n"))
        large = self.root / "src" / "large.py"
        large.write_bytes(b"x" * (512 * 1024 + 1))
        with self.assertRaisesRegex(PatchSynthesizerError, "PATCH_TARGET_TOO_LARGE"):
            synthesize_structured_patch(self.root, self._payload(path="src/large.py", find="x", replace="y"))


if __name__ == "__main__":
    unittest.main()
