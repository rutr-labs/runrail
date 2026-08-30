from functools import lru_cache
from pathlib import Path

from platformdirs import user_data_path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_home() -> Path:
    """Per-user application-data directory, chosen per OS by platformdirs.

    macOS:   ~/Library/Application Support/RunRail
    Linux:   ~/.local/share/RunRail   (honours $XDG_DATA_HOME)
    Windows: %LOCALAPPDATA%\\RunRail

    Overridden by RUNRAIL_HOME. A stable location (rather than ./.runrail in
    the current directory) means `pipx install runrail && runrail serve` keeps
    one database, log, and artifact store regardless of where it is launched.
    """
    return user_data_path("RunRail", appauthor=False, roaming=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RUNRAIL_", env_file=".env", extra="ignore")

    home: Path = Field(default_factory=_default_home)
    db_url: str | None = None
    host: str = "127.0.0.1"
    port: int = 8080
    worker_poll_seconds: float = 1.0
    worker_concurrency: int = 4
    # Max tasks of a single run executing at once (independent DAG branches).
    task_parallelism: int = 4
    browse_root: Path = Path.home()
    # When set, finished runs older than this many days (and their log/artifact
    # files) are deleted automatically by the scheduler. Essential for
    # high-frequency schedules that create hundreds of runs per day.
    retention_days: int | None = None
    # Default webhook for run notifications (Slack/Teams incoming webhooks work);
    # a workflow's own notify_webhook_url overrides this.
    notify_webhook_url: str | None = None
    # Absolute base URL for links that leave the machine — the footer of an
    # exported run file, a report permalink. Set it when RunRail sits behind a
    # proxy; the fallback only resolves inside the same network.
    public_url: str | None = None

    @property
    def base_url(self) -> str:
        return (self.public_url or f"http://{self.host}:{self.port}").rstrip("/")

    @property
    def database_url(self) -> str:
        return self.db_url or f"sqlite:///{self.home.resolve() / 'runrail.db'}"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def artifacts_dir(self) -> Path:
        return self.home / "artifacts"

    @property
    def environments_dir(self) -> Path:
        return self.home / "environments"

    def ensure_directories(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.environments_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
