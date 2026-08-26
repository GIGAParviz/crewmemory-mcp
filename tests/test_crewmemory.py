from __future__ import annotations

import subprocess
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from crewmemory.config import Config, load_config, save_connection
from crewmemory.installer import install_codex, install_json_client, launch_spec
from crewmemory.store import Store, _member_filename

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None


class TeamMemoryTests(unittest.TestCase):
    def temp_directory(self):
        base = Path.cwd() / ".test-tmp"
        base.mkdir(exist_ok=True)
        return tempfile.TemporaryDirectory(dir=base, ignore_cleanup_errors=True)

    @unittest.skipIf(tomllib is None, "tomllib is built in on Python 3.11+")
    def test_codex_config_escapes_windows_values(self) -> None:
        with self.temp_directory() as directory:
            home = Path(directory)
            env = {
                "CREWMEMORY_REPO_URL": "https://example.test/acme/memory.git",
                "CREWMEMORY_USER": 'alice "qa"',
                "CREWMEMORY_PROJECT_PATH": r"C:\work\a\b",
            }
            with patch("crewmemory.installer.Path.home", return_value=home):
                path = install_codex(env)
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            entry = parsed["mcp_servers"]["crewmemory"]
            self.assertEqual(entry["env"], env)

    @unittest.skipIf(tomllib is None, "tomllib is built in on Python 3.11+")
    def test_portable_launcher_is_written_for_codex_and_json_clients(self) -> None:
        with self.temp_directory() as directory:
            home = Path(directory)
            env = {"CREWMEMORY_REPO_URL": "https://example.test/memory.git", "CREWMEMORY_USER": "alice"}
            launch = ("uvx", ["--from", "crewmemory-mcp==0.3.0", "crewmemory"])
            with patch("crewmemory.installer.Path.home", return_value=home):
                codex = install_codex(env, launch)
                cursor = install_json_client("cursor", ".cursor/mcp.json", env, launch=launch)
                opencode = install_json_client("opencode", ".config/opencode/opencode.json", env, launch=launch)

            codex_entry = tomllib.loads(codex.read_text(encoding="utf-8"))["mcp_servers"]["crewmemory"]
            self.assertEqual(codex_entry["command"], "uvx")
            self.assertEqual(codex_entry["args"], launch[1])
            self.assertEqual(__import__("json").loads(cursor.read_text())["mcpServers"]["crewmemory"]["args"], launch[1])
            self.assertEqual(
                __import__("json").loads(opencode.read_text())["mcp"]["servers"]["crewmemory"]["command"],
                ["uvx", *launch[1]],
            )

    def test_installed_connection_is_available_to_doctor_processes(self) -> None:
        with self.temp_directory() as directory:
            with patch.dict(os.environ, {"CREWMEMORY_HOME": directory}, clear=True):
                save_connection({"CREWMEMORY_REPO_URL": "https://example.test/memory.git", "CREWMEMORY_USER": "alice"})
                cfg = load_config()
            self.assertEqual(cfg.repo_url, "https://example.test/memory.git")
            self.assertEqual(cfg.user, "alice")

    def test_uvx_launch_spec_uses_requested_package(self) -> None:
        command, args = launch_spec("uvx", "git+https://example.test/crewmemory-mcp.git")
        self.assertTrue(Path(command).name.lower() in ("uvx", "uvx.exe"))
        self.assertEqual(args, ["--from", "git+https://example.test/crewmemory-mcp.git", "crewmemory"])

    def test_supported_json_clients_get_their_native_mcp_shape(self) -> None:
        with self.temp_directory() as directory:
            home = Path(directory)
            env = {"CREWMEMORY_REPO_URL": "https://example.test/memory.git", "CREWMEMORY_USER": "alice"}
            paths = {
                "claude-code": ".claude.json",
                "cursor": ".cursor/mcp.json",
                "gemini": ".gemini/settings.json",
                "windsurf": ".codeium/windsurf/mcp_config.json",
            }
            with patch("crewmemory.installer.Path.home", return_value=home):
                for client, relative_path in paths.items():
                    written = install_json_client(client, relative_path, env)
                    entry = __import__("json").loads(written.read_text())["mcpServers"]["crewmemory"]
                    self.assertEqual(entry["env"], env)
                    self.assertTrue(entry["command"])

    def test_member_controlled_names_stay_in_their_directory(self) -> None:
        self.assertEqual(_member_filename("../../outside"), "-..-outside")
        self.assertEqual(_member_filename("..."), "member")

    def test_manual_push_commits_dirty_memory_before_pushing(self) -> None:
        with self.temp_directory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            local = root / "local"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            cfg = Config(
                repo_url=remote.as_uri(),
                user="tester",
                local_path=local,
            )
            store = Store(cfg)
            note = store.path / "notes" / "offline.md"
            note.write_text("offline change\n", encoding="utf-8")

            result = store.sync_memory("push")

            self.assertIn("pushed pending changes", result)
            check = subprocess.run(
                ["git", "--git-dir", str(remote), "show", "HEAD:notes/offline.md"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(check.stdout, "offline change\n")

    def test_invalid_sync_direction_is_rejected(self) -> None:
        with self.temp_directory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            store = Store(Config(repo_url=remote.as_uri(), user="tester", local_path=root / "local"))
            self.assertEqual(
                store.sync_memory("sideways"),
                "ERROR: direction must be 'pull', 'push', or 'both'.",
            )

    def test_store_can_switch_project_and_branch_at_runtime(self) -> None:
        def make_project(path: Path, name: str) -> Path:
            subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(path), "config", "user.name", "Tester"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.email", "tester@example.test"], check=True)
            (path / "README.md").write_text(name, encoding="utf-8")
            subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)
            return path

        with self.temp_directory() as directory:
            root = Path(directory)
            remote = root / "memory.git"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            first = make_project(root / "project-one", "one")
            second = make_project(root / "project-two", "two")
            store = Store(Config(repo_url=remote.as_uri(), user="tester", local_path=root / "memory-local"))

            store.set_project(first)
            store.save_entry("note", "First", "one", [])
            store.set_project(second)
            store.save_entry("note", "Second", "two", [])

            projects = {meta["title"]: meta.get("project") for meta, _ in store.all_meta_bodies()}
            self.assertEqual(projects, {"First": "project-one", "Second": "project-two"})


if __name__ == "__main__":
    unittest.main()
