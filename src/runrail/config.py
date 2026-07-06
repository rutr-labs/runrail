from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RUNRAIL_", env_file=".env", extra="ignore")

    home: Path = Path(".runrail")
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
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.environments_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
