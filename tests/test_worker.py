"""Worker integration checks with temporary notebooks and a fake solver.

These tests never start Codex or access the configured Google Drive folder.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import threading
import unittest

from config import load_settings
from worker import Worker


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def set_toml_value(text: str, section: str, key: str, value: object) -> str:
    """Override one setting while retaining the project's complete config."""
    rendered = json.dumps(value, ensure_ascii=False)
    lines = text.splitlines()
    start = 0
    end = len(lines)
    if section:
        heading = f"[{section}]"
        try:
            start = next(i for i, line in enumerate(lines) if line.strip() == heading) + 1
        except StopIteration:
            return text.rstrip() + f"\n\n{heading}\n{key} = {rendered}\n"
    for i in range(start, len(lines)):
        if lines[i].lstrip().startswith("["):
            end = i
            break
    for i in range(start, end):
        if re.match(rf"\s*{re.escape(key)}\s*=", lines[i]):
            lines[i] = f"{key} = {rendered}"
            break
    else:
        lines.insert(end, f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def cell(cell_type: str, source: str | list[str]) -> dict:
    result = {"cell_type": cell_type, "metadata": {}, "source": source}
    if cell_type == "code":
        result.update(execution_count=None, outputs=[])
    return result


def notebook(*cells: dict) -> dict:
    return {"cells": list(cells), "metadata": {}, "nbformat": 4, "nbformat_minor": 5}


def source_of(document: dict, index: int) -> str:
    source = document["cells"][index]["source"]
    return source if isinstance(source, str) else "".join(source)


class WorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="autocolab-worker-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.watch = self.root / "notebooks"
        self.watch.mkdir()
        self.runtime = self.watch / "worker-runtime"

        config = (PROJECT_ROOT / "config.toml").read_text(encoding="utf-8-sig")
        for section, key, value in (
            ("", "watch_folder", str(self.watch)),
            ("", "recursive", True),
            ("runtime", "directory", str(self.runtime)),
            ("retry", "seconds", 0),
            ("notebook", "marker", "@bot"),
            ("notebook", "cell_types", ["code", "markdown"]),
            ("codex", "main_prompt_file", str(PROJECT_ROOT / "prompts" / "main.txt")),
            ("codex", "code_prompt_file", str(PROJECT_ROOT / "prompts" / "code.txt")),
            ("codex", "markdown_prompt_file", str(PROJECT_ROOT / "prompts" / "markdown.txt")),
        ):
            config = set_toml_value(config, section, key, value)
        self.config_file = self.root / "config.toml"
        self.config_file.write_text(config, encoding="utf-8")
        self.settings = load_settings(self.config_file)

    def write_notebook(self, relative_path: str, document: dict) -> Path:
        path = self.watch / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_notebook(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_each_scan_replaces_only_first_marker_in_cell_order(self) -> None:
        path = self.write_notebook(
            "lesson.ipynb",
            notebook(
                cell("markdown", ["Hãy nhận xét kết quả.\n", "@bot xóa cả dòng @bot này\n", "Phần tiếp theo.\n"]),
                cell("code", "# Viết hàm cộng\n@bot\nprint(add(2, 3))\n"),
            ),
        )
        responses = iter(["**Nhận xét:** kết quả đúng.", "def add(a, b):\n    return a + b"])
        calls = []

        def solve(snapshot, marker):
            calls.append((snapshot, marker))
            return next(responses)

        worker = Worker(self.settings, solve)
        self.assertEqual(worker.scan_once(), "saved")
        first = self.read_notebook(path)
        self.assertEqual(source_of(first, 0), "Hãy nhận xét kết quả.\n**Nhận xét:** kết quả đúng.\nPhần tiếp theo.\n")
        self.assertEqual(source_of(first, 1), "# Viết hàm cộng\n@bot\nprint(add(2, 3))\n")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1].cell_type, "markdown")

        self.assertEqual(worker.scan_once(), "saved")
        second = self.read_notebook(path)
        self.assertEqual(source_of(second, 0), source_of(first, 0))
        self.assertEqual(source_of(second, 1), "# Viết hàm cộng\ndef add(a, b):\n    return a + b\nprint(add(2, 3))\n")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1].cell_type, "code")
        self.assertIn("**Nhận xét:** kết quả đúng.", calls[1][0].context())
        self.assertEqual(worker.scan_once(), "idle")
        self.assertEqual(len(calls), 2)

    def test_hash_conflict_discards_result_and_next_scan_uses_current_file(self) -> None:
        path = self.write_notebook("changed.ipynb", notebook(cell("code", "# cũ\n@bot\n")))
        later_path = self.write_notebook("z_later.ipynb", notebook(cell("code", "@bot\n")))
        later_bytes = later_path.read_bytes()
        modified = notebook(cell("markdown", "Ngữ cảnh mới\n@bot\n"))
        modified_bytes = json.dumps(modified, ensure_ascii=False, indent=4).encode("utf-8")
        calls = []

        def solve(snapshot, marker):
            calls.append((snapshot, marker))
            if len(calls) == 1:
                path.write_bytes(modified_bytes)
                return "stale_result = True"
            return "Câu trả lời cho ngữ cảnh mới."

        worker = Worker(self.settings, solve)
        self.assertEqual(worker.scan_once(), "conflict")
        self.assertEqual(path.read_bytes(), modified_bytes)
        self.assertEqual(later_path.read_bytes(), later_bytes)
        self.assertEqual(len(calls), 1)
        self.assertEqual(worker.scan_once(), "saved")
        result = source_of(self.read_notebook(path), 0)
        self.assertEqual(result, "Ngữ cảnh mới\nCâu trả lời cho ngữ cảnh mới.\n")
        self.assertNotIn("stale_result", path.read_text(encoding="utf-8"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1].cell_type, "markdown")
        self.assertIn("Ngữ cảnh mới", calls[1][0].context())
        self.assertEqual(later_path.read_bytes(), later_bytes)

    def test_solver_failure_preserves_file_and_tries_next_eligible_file(self) -> None:
        failing_path = self.write_notebook("a_failure.ipynb", notebook(cell("code", "@bot\n")))
        good_path = self.write_notebook("b_success.ipynb", notebook(cell("code", "@bot\n")))
        failing_bytes = failing_path.read_bytes()
        calls = []

        def solve(snapshot, marker):
            calls.append((snapshot, marker))
            if len(calls) == 1:
                raise RuntimeError("simulated Codex failure")
            return "answer = 42"

        worker = Worker(self.settings, solve)
        self.assertEqual(worker.scan_once(), "saved")
        self.assertEqual(failing_path.read_bytes(), failing_bytes)
        self.assertEqual(source_of(self.read_notebook(good_path), 0), "answer = 42\n")
        self.assertEqual(len(calls), 2)
        self.assertEqual([snapshot.path for snapshot, _ in calls], [failing_path, good_path])

    def test_failure_without_another_candidate_returns_failed(self) -> None:
        path = self.write_notebook("only.ipynb", notebook(cell("code", "@bot\n")))
        original_bytes = path.read_bytes()

        def solve(snapshot, marker):
            raise RuntimeError("simulated Codex failure")

        worker = Worker(self.settings, solve)
        self.assertEqual(worker.scan_once(), "failed")
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_checkpoint_and_runtime_directories_are_excluded(self) -> None:
        checkpoint_path = self.write_notebook(".ipynb_checkpoints/ignored.ipynb", notebook(cell("code", "@bot\n")))
        nested_checkpoint_path = self.write_notebook("nested/.ipynb_checkpoints/ignored.ipynb", notebook(cell("code", "@bot\n")))
        runtime_path = self.write_notebook("worker-runtime/nested/ignored.ipynb", notebook(cell("code", "@bot\n")))
        eligible_path = self.write_notebook("z_eligible.ipynb", notebook(cell("code", "@bot\n")))
        ignored = {path: path.read_bytes() for path in (checkpoint_path, nested_checkpoint_path, runtime_path)}
        calls = []

        def solve(snapshot, marker):
            calls.append((snapshot, marker))
            return "answer = 42"

        worker = Worker(self.settings, solve)
        self.assertEqual(worker.scan_once(), "saved")
        self.assertEqual(source_of(self.read_notebook(eligible_path), 0), "answer = 42\n")
        self.assertEqual(len(calls), 1)
        self.assertEqual(worker.scan_once(), "idle")
        self.assertEqual(len(calls), 1)
        for path, original_bytes in ignored.items():
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_preexisting_stop_request_does_not_call_solver_or_write(self) -> None:
        path = self.write_notebook("untouched.ipynb", notebook(cell("code", "@bot\n")))
        original_bytes = path.read_bytes()
        stop_event = threading.Event()
        stop_event.set()
        calls = []

        def solve(snapshot, marker):
            calls.append((snapshot, marker))
            return "must_not_be_inserted = True"

        worker = Worker(self.settings, solve, stop_event=stop_event)
        self.assertEqual(worker.scan_once(), "stopped")
        self.assertEqual(calls, [])
        self.assertEqual(path.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
