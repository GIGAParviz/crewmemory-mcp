from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    repo_url: str
    user: str
    token: str | None = None
    email: str | None = None
    branch: str | None = None
    local_path: Path | None = None
    project_path: Path | None = None

    @property
    def repo_name(self) -> str:
        return repo_name_from_url(self.repo_url)

    @property
    def personal_root(self) -> Path:
        return default_root() / "personal"


def default_root() -> Path:
    return Path(os.environ.get("CREWMEMORY_HOME", "").strip() or Path.home() / ".crewmemory")


def connection_path() -> Path:
    return default_root() / "config.json"


def save_connection(values: dict[str, str]) -> Path:
    """Persist installer values so CLI commands work outside an MCP client."""
    path = connection_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(values, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def _saved_connection() -> dict[str, str]:
    path = connection_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def repo_name_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    name = Path(path).name if path else ""
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name or "crewmemory"


def authenticated_url(url: str, token: str | None) -> str:
    if not token:
        return url
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return url
    userinfo = "x-access-token" if parsed.hostname == "github.com" else "oauth2"
    netloc = f"{userinfo}:{token}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def redact(text: str, token: str | None) -> str:
    if token and token in text:
        text = text.replace(token, "***")
    return text


def load_config() -> Config:
    saved = _saved_connection()

    def value(name: str) -> str:
        return os.environ.get(name, "").strip() or saved.get(name, "").strip()

    repo_url = value("CREWMEMORY_REPO_URL")
    user = value("CREWMEMORY_USER")

    missing = []
    if not repo_url:
        missing.append("CREWMEMORY_REPO_URL")
    if not user:
        missing.append("CREWMEMORY_USER")
    if missing:
        raise ConfigError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Run 'crewmemory install <client>' to configure automatically."
        )

    local_path_env = value("CREWMEMORY_LOCAL_PATH")
    local_path = (
        Path(local_path_env).expanduser()
        if local_path_env
        else default_root() / repo_name_from_url(repo_url)
    )

    project_env = value("CREWMEMORY_PROJECT_PATH")
    project_path = Path(project_env).expanduser() if project_env else None

    return Config(
        repo_url=repo_url,
        user=user,
        token=value("CREWMEMORY_TOKEN") or None,
        email=value("CREWMEMORY_EMAIL") or None,
        branch=value("CREWMEMORY_BRANCH") or None,
        local_path=local_path,
        project_path=project_path,
    )
