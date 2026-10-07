"""Verify notebook editing against small, isolated notebook fixtures."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from notebooks import load_snapshot, save_if_unchanged


def notebook(cells):
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3"},
            "custom": {"keep": True},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def code_cell(source, **extra):
    return {
        "id": "preserved-code-id",
        "cell_type": "code",
        "source": source,
        "metadata": {"tags": ["retain-this"]},
        "execution_count": 8,
        "outputs": [{"output_type": "stream", "name": "stdout", "text": ["old output\n"]}],
        **extra,
    }


def text_cell(source, kind="markdown"):
    return {"id": "text-id", "cell_type": kind, "source": source, "metadata": {}}


class NotebookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sample.ipynb"

    def write(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return load_snapshot(self.path)

    def test_first_marker_follows_cell_then_line_order(self):
        snapshot = self.write(notebook([
            text_cell("@bot in a raw cell", kind="raw"),
            code_cell(["ordinary code\n"], outputs=[
                {"output_type": "stream", "name": "stdout", "text": "@bot output"}
            ]),
            text_cell(["Nhận xét kết quả\n", "prefix @bot extra words\n", "@bot later\n"]),
            code_cell("@bot\n"),
        ]))
        marker = snapshot.first_marker()
        self.assertIsNotNone(marker)
        self.assertEqual((marker.cell_index, marker.line_index, marker.cell_type), (2, 1, "markdown"))
        code_marker = snapshot.first_marker(cell_types=("code",))
        self.assertEqual((code_marker.cell_index, code_marker.line_index), (3, 0))

    def test_no_marker_in_eligible_sources_returns_none(self):
        data = notebook([text_cell("@bot", kind="raw"), code_cell("answer = 1\n")])
        data["metadata"]["description"] = "@bot"
        data["cells"][1]["outputs"][0]["text"] = ["@bot\n"]
        self.assertIsNone(self.write(data).first_marker())

    def test_replaces_entire_first_line_and_preserves_all_other_content(self):
        data = notebook([
            code_cell(["def calculate():\r\n", "    @bot discard @bot all of this\r\n", "    return result\r\n"]),
            text_cell("A second question\n@bot\n"),
        ])
        snapshot = self.write(data)
        original = copy.deepcopy(snapshot.notebook)
        updated = snapshot.replace(snapshot.first_marker(), "    result = 2\n    result += 3")

        self.assertIsInstance(updated["cells"][0]["source"], list)
        source = "".join(updated["cells"][0]["source"])
        self.assertEqual(source.splitlines(), [
            "def calculate():", "    result = 2", "    result += 3", "    return result"
        ])
        expected = copy.deepcopy(original)
        expected["cells"][0]["source"] = updated["cells"][0]["source"]
        self.assertEqual(updated, expected)
        self.assertEqual(snapshot.notebook, original)

    def test_markdown_string_source_accepts_raw_markdown_response(self):
        snapshot = self.write(notebook([text_cell("Câu hỏi\n@bot bỏ dòng này\nKết thúc")]))
        result = "**Nhận xét:** kết quả tốt.\n\n- Điểm thứ nhất\n- Điểm thứ hai"
        updated = snapshot.replace(snapshot.first_marker(), result)
        source = updated["cells"][0]["source"]
        self.assertIsInstance(source, str)
        self.assertEqual(source, "Câu hỏi\n" + result + "\nKết thúc")

    def test_final_marker_without_newline_and_response_with_blank_lines(self):
        snapshot = self.write(notebook([code_cell("before = 1\n@bot")]))
        updated = snapshot.replace(snapshot.first_marker(), "first = 2\n\nsecond = 3")
        self.assertEqual(updated["cells"][0]["source"].splitlines(), [
            "before = 1", "first = 2", "", "second = 3"
        ])

    def test_context_preserves_cell_order_and_omits_outputs_by_default(self):
        snapshot = self.write(notebook([
            text_cell("Đề bài độc nhất"),
            code_cell(["input_value_unique = 7\n"], outputs=[
                {"output_type": "stream", "name": "stderr", "text": ["private diagnostic unique\n"]}
            ]),
            text_cell("Nhận xét độc nhất\n@bot"),
        ]))
        context = snapshot.context()
        positions = [context.index(value) for value in (
            "Đề bài độc nhất", "input_value_unique = 7", "Nhận xét độc nhất"
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("private diagnostic unique", context)
        self.assertIn("private diagnostic unique", snapshot.context(include_outputs=True))

    def test_save_writes_updated_notebook_when_snapshot_is_unchanged(self):
        snapshot = self.write(notebook([code_cell(["@bot\n", "print(answer)\n"])]))
        updated = snapshot.replace(snapshot.first_marker(), "answer = 42")
        self.assertTrue(save_if_unchanged(snapshot, updated))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), updated)
        self.assertIsNone(load_snapshot(self.path).first_marker())

    def test_hash_conflict_on_metadata_edit_keeps_latest_file_untouched(self):
        snapshot = self.write(notebook([code_cell("@bot\n")]))
        updated = snapshot.replace(snapshot.first_marker(), "answer = 42")
        external = copy.deepcopy(snapshot.notebook)
        external["metadata"]["custom"]["edited_by_user"] = True
        latest = json.dumps(external, ensure_ascii=False, indent=4).encode("utf-8")
        self.path.write_bytes(latest)

        self.assertFalse(save_if_unchanged(snapshot, updated))
        self.assertEqual(self.path.read_bytes(), latest)


if __name__ == "__main__":
    unittest.main()
