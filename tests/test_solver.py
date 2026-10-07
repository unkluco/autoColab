"""Exercise CLI transport with a local fake process, never the real Codex CLI."""

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from config import load_settings
from notebooks import load_snapshot
from solver import (CodexSolver, SolverCleanupError, SolverError, SolverInputError,
                    SolverResponseError, SolverStopped, _stop_process_tree)


FAKE_CLI = r'''
import json
import os
from pathlib import Path
import sys
import time

arguments = sys.argv[1:]
mode = os.environ.get("AUTOCOLAB_TEST_MODE", "ok")
if mode == "no-input":
    time.sleep(60)
context = sys.stdin.buffer.read().decode("utf-8")
capture = Path(os.environ["AUTOCOLAB_TEST_CAPTURE"])
capture.write_text(json.dumps({"arguments": arguments, "context": context}), encoding="utf-8")
print("NOISY PROGRESS: this must never become the inserted answer", flush=True)
print("diagnostic from stderr", file=sys.stderr, flush=True)
if mode == "timeout":
    time.sleep(60)
if mode == "noisy":
    sys.stdout.buffer.write(b"x" * (1024 * 1024))
    sys.stdout.buffer.flush()
if mode != "missing":
    answer = " \n\t" if mode == "empty" else os.environ["AUTOCOLAB_TEST_ANSWER"]
    response = Path(arguments[arguments.index("--output-last-message") + 1])
    response.write_bytes(answer.encode("utf-8"))
if mode == "failure":
    raise SystemExit(9)
'''


class SolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autocolab-solver-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.watch = self.root / "notebooks"
        self.watch.mkdir()
        self.runtime = self.root / "runtime"
        self.capture = self.root / "capture.json"
        self.script = self.root / "fake_cli.py"
        self.script.write_text(FAKE_CLI, encoding="utf-8")
        self.prompts = {}
        for name, contents in (
            ("main", "MAIN instructions: thay dòng @bot bằng câu trả lời."),
            ("code", "CODE instructions: trả code thuần."),
            ("markdown", "MARKDOWN instructions: trả nhận xét bằng Markdown."),
        ):
            self.prompts[name] = self.root / (name + ".txt")
            self.prompts[name].write_text(contents, encoding="utf-8")

        config = self.root / "config.toml"
        values = lambda path: json.dumps(str(path), ensure_ascii=False)
        config.write_text(
            f"watch_folder = {values(self.watch)}\n"
            "[codex]\n"
            f"command = {values(sys.executable)}\n"
            f"main_prompt_file = {values(self.prompts['main'])}\n"
            f"code_prompt_file = {values(self.prompts['code'])}\n"
            f"markdown_prompt_file = {values(self.prompts['markdown'])}\n"
            "disable_mcp_servers = false\n"
            "timeout_seconds = 1\n"
            "[runtime]\n"
            f"directory = {values(self.runtime)}\n",
            encoding="utf-8",
        )
        self.settings = load_settings(config)
        self.environment = {
            "AUTOCOLAB_TEST_CAPTURE": str(self.capture),
            "AUTOCOLAB_TEST_ANSWER": "answer = 'đúng ✓'\n",
            "AUTOCOLAB_TEST_MODE": "ok",
        }

    def snapshot(self, cell_type="code"):
        path = self.watch / "example.ipynb"
        data = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {},
            "cells": [
                {"cell_type": "markdown", "source": "Đề bài: nhận xét dữ liệu tiếng Việt", "metadata": {}},
                {"cell_type": cell_type, "source": ["Ngữ cảnh quanh vị trí chèn\n", "@bot\n"], "metadata": {}},
                {"cell_type": "code", "source": "print('café')\n", "metadata": {},
                 "outputs": [{"output_type": "stream", "name": "stdout", "text": "omitted output"}]},
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return load_snapshot(path)

    def fake_solver(self, settings=None):
        solver = CodexSolver(settings or self.settings)
        solver.command = [sys.executable, str(self.script)]
        return solver

    def invoke(self, cell_type="code", mode="ok", answer=None, settings=None):
        snapshot = self.snapshot(cell_type)
        environment = dict(self.environment, AUTOCOLAB_TEST_MODE=mode)
        if answer is not None:
            environment["AUTOCOLAB_TEST_ANSWER"] = answer
        with patch.dict(os.environ, environment):
            return self.fake_solver(settings)(snapshot, snapshot.first_marker())

    def captured_instructions(self):
        captured = json.loads(self.capture.read_text(encoding="utf-8"))
        arguments = captured["arguments"]
        overrides = [arguments[i + 1] for i, value in enumerate(arguments) if value == "-c"]
        encoded = next(value.split("=", 1)[1] for value in overrides if value.startswith("developer_instructions="))
        return captured, json.loads(encoded)

    def test_utf8_context_and_code_instructions_use_final_answer_file(self):
        answer = "résultat = 'đúng ✓'\n"
        self.assertEqual(self.invoke(answer=answer), answer)
        captured, instructions = self.captured_instructions()
        self.assertIn("Đề bài: nhận xét dữ liệu tiếng Việt", captured["context"])
        self.assertIn("print('café')", captured["context"])
        self.assertIn("Target: Cell 2, line 2, type code.", captured["context"])
        self.assertNotIn("omitted output", captured["context"])
        self.assertIn("MAIN instructions", instructions)
        self.assertIn("CODE instructions", instructions)
        self.assertNotIn("MARKDOWN instructions", instructions)
        self.assertEqual(captured["arguments"][-1], "-")
        log = (self.runtime / "codex-last.log").read_text(encoding="utf-8")
        self.assertIn("NOISY PROGRESS", log)
        self.assertIn("diagnostic from stderr", log)

    def test_markdown_uses_type_specific_prompt_and_preserves_raw_response(self):
        answer = "**Nhận xét:** kết quả tốt.\n\n- Tính toán đúng.\n"
        self.assertEqual(self.invoke(cell_type="markdown", answer=answer), answer)
        captured, instructions = self.captured_instructions()
        self.assertIn("type markdown.", captured["context"])
        self.assertIn("MAIN instructions", instructions)
        self.assertIn("MARKDOWN instructions", instructions)
        self.assertNotIn("CODE instructions", instructions)

    def test_mcp_override_uses_unquoted_cli_path(self):
        codex_home = self.root / "fake-codex-home"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            '[mcp_servers.node_repl]\ncommand = "unused"\n', encoding="utf-8"
        )
        settings = replace(self.settings, disable_mcp_servers=True)
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            arguments = self.fake_solver(settings).build_arguments(
                self.root / "answer.txt", self.root / "scratch", "Instructions"
            )
        self.assertIn("mcp_servers.node_repl.enabled=false", arguments)
        self.assertNotIn('mcp_servers."node_repl".enabled=false', arguments)

    def test_nonzero_exit_rejects_response(self):
        with self.assertRaisesRegex(SolverError, "exit 9"):
            self.invoke(mode="failure")

    def test_empty_response_is_rejected(self):
        with self.assertRaisesRegex(SolverResponseError, "empty answer"):
            self.invoke(mode="empty")

    def test_missing_response_is_rejected_even_with_noisy_stdout(self):
        with self.assertRaisesRegex(SolverResponseError, "did not write a final answer"):
            self.invoke(mode="missing")

    def test_timeout_stops_fake_process_and_cleans_scratch_directory(self):
        started = time.monotonic()
        with self.assertRaisesRegex(SolverError, "timed out"):
            self.invoke(mode="timeout")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(list((self.runtime / "calls").iterdir()), [])

    def test_oversized_context_is_an_input_error_without_starting_a_process(self):
        with self.assertRaisesRegex(SolverInputError, "context has"):
            self.invoke(settings=replace(self.settings, context_max_chars=128))
        self.assertFalse(self.capture.exists())
        self.assertFalse((self.runtime / "calls").exists())

    def test_cli_log_has_a_hard_byte_limit_and_overflow_rejects_answer(self):
        with self.assertRaisesRegex(SolverError, "log exceeded"):
            self.invoke(mode="noisy", settings=replace(self.settings, codex_log_max_bytes=1024))
        self.assertEqual((self.runtime / "codex-last.log").stat().st_size, 1024)
        self.assertEqual(list((self.runtime / "calls").iterdir()), [])

    def test_stop_event_cancels_a_running_cli(self):
        snapshot = self.snapshot()
        solver = self.fake_solver(settings=replace(self.settings, timeout_seconds=30))
        timer = threading.Timer(0.3, solver.stop_event.set)
        self.addCleanup(timer.cancel)
        timer.start()
        started = time.monotonic()
        with patch.dict(os.environ, dict(self.environment, AUTOCOLAB_TEST_MODE="timeout")):
            with self.assertRaisesRegex(SolverStopped, "Worker stopped"):
                solver(snapshot, snapshot.first_marker())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(list((self.runtime / "calls").iterdir()), [])

    def test_cli_that_never_reads_stdin_still_times_out(self):
        snapshot = self.snapshot()
        snapshot.notebook['cells'][0]['source'] = 'large input ' * 100000
        solver = self.fake_solver()
        started = time.monotonic()
        with patch.dict(os.environ, dict(self.environment, AUTOCOLAB_TEST_MODE="no-input")):
            with self.assertRaisesRegex(SolverError, "timed out"):
                solver(snapshot, snapshot.first_marker())
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(list((self.runtime / "calls").iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows taskkill cancellation')
    def test_taskkill_timeout_does_not_make_cleanup_wait_forever(self):
        import subprocess
        from unittest.mock import Mock
        process = Mock(pid=123, args=['fake'])
        process.poll.return_value = None
        thread = Mock(name='io-thread')
        thread.is_alive.return_value = False
        with patch('solver.subprocess.run', side_effect=subprocess.TimeoutExpired(['taskkill'], 5)) as run:
            error = _stop_process_tree(process, (thread,))
        self.assertIn('taskkill timed out', error)
        self.assertEqual(run.call_args.kwargs['timeout'], 5)
        process.kill.assert_called_once()
        self.assertLessEqual(process.wait.call_args.kwargs['timeout'], 5)
        self.assertLessEqual(thread.join.call_args.kwargs['timeout'], 5)

    def test_invalid_utf8_answer_is_a_solver_error(self):
        script = self.script.read_text(encoding='utf-8')
        self.script.write_text(script.replace('answer.encode("utf-8")', 'b"\\xff"'), encoding='utf-8')
        with self.assertRaisesRegex(SolverResponseError, 'Cannot read Codex final answer'):
            self.invoke()

    def test_oversized_final_answer_is_rejected_without_a_partial_result(self):
        with self.assertRaisesRegex(SolverResponseError, 'final answer exceeded'):
            self.invoke(answer='x' * 5000, settings=replace(self.settings, context_max_chars=1000))

    def test_runtime_creation_failure_uses_shared_solver_error_class(self):
        with patch('solver.tempfile.TemporaryDirectory', side_effect=PermissionError('runtime locked')):
            with self.assertRaisesRegex(SolverError, 'runtime locked'):
                self.invoke()

    def test_invalid_unicode_context_is_rejected_before_spawning(self):
        snapshot = self.snapshot()
        snapshot.notebook['cells'][0]['source'] = '\ud800'
        with patch('solver.subprocess.Popen') as launch:
            with self.assertRaisesRegex(SolverInputError, 'invalid Unicode'):
                self.fake_solver()(snapshot, snapshot.first_marker())
        launch.assert_not_called()

    def test_incomplete_cleanup_is_fatal_and_preserves_the_original_timeout(self):
        def stop_then_report(process, threads):
            _stop_process_tree(process, threads)
            return 'descendant pipe is still open'
        with patch('solver._stop_process_tree', side_effect=stop_then_report):
            with self.assertRaisesRegex(SolverCleanupError, 'timed out.*cleanup did not complete'):
                self.invoke(mode='timeout')
        self.assertEqual(list((self.runtime / 'calls').iterdir()), [])

    def test_io_thread_start_failure_cleans_up_the_spawned_process(self):
        processes = []
        original_launch = subprocess.Popen
        original_start = threading.Thread.start

        def launch(*args, **kwargs):
            process = original_launch(*args, **kwargs)
            processes.append(process)
            return process

        def start(thread):
            if thread.name == 'codex-input':
                raise RuntimeError('threads unavailable')
            return original_start(thread)

        with patch('solver.subprocess.Popen', side_effect=launch), patch('solver.threading.Thread.start', start):
            with self.assertRaisesRegex(SolverError, 'Cannot start Codex input writer'):
                self.invoke()
        self.assertIsNotNone(processes[0].poll())
        self.assertEqual(list((self.runtime / 'calls').iterdir()), [])


if __name__ == "__main__":
    unittest.main()
