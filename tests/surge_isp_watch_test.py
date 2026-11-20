"""Real macOS event tests; fixtures only, no real credentials or Surge calls."""
import importlib.util
import json
import os
from pathlib import Path
import select
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("isp_watch", ROOT / "scripts/surge_isp_watch.py")
watcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watcher)


class InputsTests(unittest.TestCase):
    def test_missing_registry_and_disabled_bindings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, marker = root / "config", root / "marker"
            self.assertEqual(watcher.inputs(config, root, marker), {config, root / "bindings.json", marker})
            (root / "bindings.json").write_text('{"x":{"enabled":false}}')
            self.assertEqual(len(watcher.inputs(config, root, marker)), 3)

    def test_same_bytes_are_not_a_change(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "file"
            p.write_text("same")
            before = watcher.snapshot({p})
            os.utime(p, None)
            self.assertEqual(watcher.snapshot({p}), before)

    def test_failed_sync_retries_are_bounded_then_block(self):
        stop = threading.Event()
        events = mock.Mock()
        waits = []

        def wait(timeout):
            waits.append(timeout)
            if len(waits) == 4:
                stop.set()
            return False

        events.wait.side_effect = wait
        sync = mock.Mock(return_value=False)
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(watcher, "Events", return_value=events):
            root = Path(folder)
            watcher.watch(root / "config", root, root / "marker", sync, stop=stop)
        self.assertEqual(sync.call_count, 4)
        self.assertEqual(waits, [2, 4, 8, None])


@unittest.skipUnless(hasattr(select, "kqueue"), "macOS/BSD kernel events required")
class KernelEventsTests(unittest.TestCase):
    def test_inplace_atomic_replacement_and_recreated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.write_text("one")
            events = watcher.Events()
            try:
                events.refresh({source})
                source.write_text("two")
                self.assertTrue(events.wait(1), "in-place write must notify")
                replacement = root / "replacement"
                replacement.write_text("three")
                os.replace(replacement, source)
                self.assertTrue(events.wait(1), "atomic replacement must notify")
                events.refresh({source})
                source.write_text("four")
                self.assertTrue(events.wait(1), "must rebind to replacement inode")
                source.unlink()
                self.assertTrue(events.wait(1))
                events.refresh({source})
                source.write_text("five")
                self.assertTrue(events.wait(1), "parent catches recreation")
            finally:
                events.close()

    def test_missing_parent_creation_is_detected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing" / "marker"
            events = watcher.Events()
            try:
                events.refresh({path})
                path.parent.mkdir()
                self.assertTrue(events.wait(1))
                events.refresh({path})
                path.write_text("event")
                self.assertTrue(events.wait(1))
            finally:
                events.close()

    def test_loop_idle_burst_hook_and_changes_during_sync(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config, marker = root / "config", root / "marker"
            config.write_text("initial")
            calls, errors = [], []
            stop = threading.Event()

            def sync():
                calls.append(config.read_text())
                if calls[-1] == "during":
                    config.write_text("changed-during-sync")
                return True

            def run():
                try:
                    watcher.watch(config, root, marker, sync, stop=stop, debounce=0.05)
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()

            def await_count(n):
                deadline = time.monotonic() + 3
                while len(calls) < n and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(len(calls), n, errors)

            try:
                await_count(1)
                time.sleep(0.15)
                self.assertEqual(len(calls), 1, "idle must not sync")
                (root / "irrelevant-output").write_text("output")
                time.sleep(0.15)
                self.assertEqual(len(calls), 1, "unrelated output must not loop")
                for i in range(10):
                    config.write_text(str(i))
                await_count(2)
                time.sleep(0.15)
                self.assertEqual(len(calls), 2, "burst should coalesce")
                self.assertEqual(calls[-1], "9")
                marker.write_text("reload")
                await_count(3)
                config.write_text("during")
                await_count(5)
                self.assertEqual(calls[-1], "changed-during-sync")
            finally:
                stop.set()
                marker.write_text("stop")
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
