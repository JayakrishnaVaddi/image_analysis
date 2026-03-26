"""
Project-local environment loading helpers.
"""

from __future__ import annotations

import os
from pathlib import Path


ENV_FILE_PATH = Path(__file__).resolve().parent / ".env"


def load_local_env(env_path: Path = ENV_FILE_PATH) -> None:
    """
    Load key/value pairs from the project-local `.env` file.

    Values already present in the process environment are preserved so shell
    overrides continue to work as expected.
    """

    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value
