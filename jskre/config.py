"""Config loading.

YAML if PyYAML is installed, otherwise a small JSON fallback so the tool still
runs on a bare Python. Missing file is not an error -- defaults are usable.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def load_config(path: str | Path = "config.yml") -> dict:
    path = Path(path)
    if not path.exists():
        log.info("No config at %s; using built-in defaults", path)
        return {}

    text = path.read_text(encoding="utf-8")

    if path.suffix in (".yml", ".yaml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            log.warning(
                "PyYAML not installed; cannot read %s. "
                "Install pyyaml or use a .json config.",
                path,
            )
            return {}
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text) if text.strip() else {}

    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data
