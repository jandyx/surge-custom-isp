#!/usr/bin/env python3
"""Event-driven ISP sync. No network listener, third-party packages or idle timer."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

MARKER = "isp-sync-event"


def inputs(config, state, marker):
    paths = {config, state / "bindings.json", marker}
    try:
        bindings = json.loads((state / "bindings.json").read_text())
        for binding in bindings.values():
            if binding.get("enabled"):
                paths.update(Path(binding[k]) for k in ("source", "profile", "generated"))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        # Keep listening to the registry while an interrupted write is repaired.
        pass
    return paths


def snapshot(paths):
    result = {}
    for path in paths:
        try:
            result[str(path)] = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            result[str(path)] = None
    return result


class Events:
    """Watch files AND parents: file writes and atomic replacements both matter."""
    def __init__(self):
        self.queue = select.kqueue()
        self.fds = {}

    def refresh(self, paths):
        desired = set(paths)
        for path in paths:
            parent = path.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            desired.add(parent)
        for path, fd in list(self.fds.items()):
            try:
                current = path.stat()
                opened = os.fstat(fd)
                same = (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
            except OSError:
                same = False
            if path not in desired or not same:
                os.close(fd)
                del self.fds[path]
        for path in desired - self.fds.keys():
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_EVTONLY", 0))
            except OSError:
                continue
            try:
                event = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE |
                    select.KQ_NOTE_RENAME | select.KQ_NOTE_ATTRIB | select.KQ_NOTE_REVOKE)
                self.queue.control([event], 0, 0)
                self.fds[path] = fd
            except BaseException:
                os.close(fd)
                raise

    def wait(self, timeout=None):
        return bool(self.queue.control(None, max(1, len(self.fds)), timeout))

    def close(self):
        for fd in self.fds.values():
            os.close(fd)
        self.queue.close()


def watch(config, state, marker, sync, *, stop=None, debounce=0.5):
    events = Events()
    previous = None
    failures = 0
    retry = False
    try:
        while stop is None or not stop.is_set():
            paths = inputs(config, state, marker)
            events.refresh(paths)
            current = snapshot(paths)
            if current != previous or retry:
                if current != previous:
                    failures = 0
                previous = current
                ok = sync()
                failures = 0 if ok else failures + 1
                retry = not ok and failures <= 3
                # Recheck immediately after work: an input may change during sync.
                if snapshot(inputs(config, state, marker)) != current:
                    continue
            # Healthy idle blocks indefinitely. Timed retries only follow errors.
            timeout = min(30, 2 ** failures) if retry else None
            if events.wait(timeout):
                # Fixed coalescing window, not a recurring polling interval.
                time.sleep(debounce)
    finally:
        events.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    marker = Path.home() / "Library/Application Support/com.nssurge.surge-mac/SGJSVMPersistentStore" / MARKER
    script = Path(__file__).with_name("surge-isp.py")

    def sync():
        print("[isp-watch] event/startup: sync", flush=True)
        try:
            result = subprocess.run([sys.executable, str(script), "--config", str(args.config),
                "--state-dir", str(args.state_dir), "sync"], timeout=120)
            print(f"[isp-watch] sync exit={result.returncode}", flush=True)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            print("[isp-watch] sync unavailable/timed out; bounded retry", flush=True)
            return False

    watch(args.config, args.state_dir, marker, sync)


if __name__ == "__main__":
    main()
