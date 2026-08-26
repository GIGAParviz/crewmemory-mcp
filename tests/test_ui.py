from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import urllib.request
from pathlib import Path

from crewmemory.config import Config
from crewmemory.store import Store


def _make_store(root: Path, user: str) -> Store:
    remote = root / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    return Store(Config(repo_url=remote.as_uri(), user=user, local_path=root / "local"))


class SnapshotTests(unittest.TestCase):
    def temp_directory(self):
        base = Path.cwd() / ".test-tmp"
        base.mkdir(exist_ok=True)
        return tempfile.TemporaryDirectory(dir=base, ignore_cleanup_errors=True)

    def test_snapshot_contains_structured_views(self) -> None:
        with self.temp_directory() as directory:
            root = Path(directory)
            store = _make_store(root, "tester")
            store.save_entry("note", "Snapshot note", "hello dashboard", ["ui"])
            store.update_status("Building the UI", progress=25, blockers=["review"])

            snap = store.snapshot()

            self.assertEqual(snap["scope"], "team")
            self.assertEqual(len(snap["entries"]), 1)
            entry = snap["entries"][0]
            self.assertEqual(entry["title"], "Snapshot note")
            self.assertEqual(entry["type"], "note")
            self.assertEqual(entry["status"], "unverified")
            self.assertIn("confidence", entry)
            self.assertEqual(len(snap["statuses"]), 1)
            status = snap["statuses"][0]
            self.assertEqual(status["task"], "Building the UI")
            self.assertEqual(status["progress"], 25)
            self.assertEqual(status["blockers"], ["review"])
            self.assertFalse(status["stale"])
            actions = [e["action"] for e in snap["activity"]]
            self.assertIn("note", actions)
            self.assertIn("status", actions)


class DashboardHttpTests(unittest.TestCase):
    def temp_directory(self):
        base = Path.cwd() / ".test-tmp"
        base.mkdir(exist_ok=True)
        return tempfile.TemporaryDirectory(dir=base, ignore_cleanup_errors=True)

    def test_server_serves_html_and_api(self) -> None:
        from crewmemory.ui import UiServer

        with self.temp_directory() as directory:
            root = Path(directory)
            _make_store(root, "tester")
            ui = UiServer()
            server = __import__("http.server", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer(
                ("127.0.0.1", 0), ui.handler()
            )
            port = server.server_address[1]
            thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                import os

                env_backup = os.environ.get("CREWMEMORY_REPO_URL")
                os.environ["CREWMEMORY_REPO_URL"] = (root / "remote.git").as_uri()
                os.environ["CREWMEMORY_USER"] = "tester"
                os.environ["CREWMEMORY_LOCAL_PATH"] = str(root / "local")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                        html = resp.read().decode("utf-8")
                    self.assertIn("Crew Memory", html)
                    self.assertIn("/api/all", html)

                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/all") as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                    self.assertIn("entries", payload["team"])
                    self.assertIn("statuses", payload["team"])
                    self.assertIn("repo_name", payload)
                finally:
                    if env_backup is None:
                        os.environ.pop("CREWMEMORY_REPO_URL", None)
                    else:
                        os.environ["CREWMEMORY_REPO_URL"] = env_backup
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
