"""adopt legacy app-created environments"""

import json
import subprocess
from pathlib import Path

import sqlalchemy as sa
from alembic import op

from runrail.config import get_settings

revision = "e2f96a3d710b"
down_revision = "d7a4b18c2f10"
branch_labels = None
depends_on = None


def _base_python(environment_root: Path) -> str | None:
    config = environment_root / "pyvenv.cfg"
    if not config.is_file():
        return None
    for line in config.read_text(errors="replace").splitlines():
        if line.lower().startswith("executable ="):
            return line.split("=", 1)[1].strip()
    return None


def _top_level_packages(python: Path) -> list[str]:
    try:
        completed = subprocess.run(
            [str(python), "-m", "pip", "list", "--not-required", "--format=json",
             "--disable-pip-version-check"],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if completed.returncode:
            return []
        ignored = {"pip", "setuptools", "wheel"}
        return [
            f"{item['name']}=={item['version']}"
            for item in json.loads(completed.stdout)
            if item["name"].lower() not in ignored
        ]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return []


def upgrade():
    connection = op.get_bind()
    root = get_settings().environments_dir.expanduser().resolve()
    rows = connection.execute(sa.text(
        "SELECT id, executable FROM environments "
        "WHERE managed = :managed AND env_type = :env_type AND executable IS NOT NULL"
    ), {"managed": False, "env_type": "python"}).mappings()
    for row in rows:
        python = Path(row["executable"]).expanduser().resolve()
        environment_root = python.parent.parent
        if environment_root.parent != root or not (environment_root / "pyvenv.cfg").is_file():
            continue
        connection.execute(sa.text(
            "UPDATE environments SET managed = :managed, base_executable = :base, "
            "packages_json = :packages, status = :status WHERE id = :id"
        ), {
            "managed": True,
            "base": _base_python(environment_root),
            "packages": json.dumps(_top_level_packages(python)),
            "status": "ready",
            "id": row["id"],
        })


def downgrade():
    # Adoption is intentionally retained: reverting it would hide management controls
    # for environments that are still owned by RunRail.
    pass
