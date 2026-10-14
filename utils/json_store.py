"""Shared helpers for durable JSON config/cache files."""
import json
import os
import tempfile
from pathlib import Path


def load_json(path, default=None):
    """Read a JSON object, returning ``default`` on any failure."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return default if data is None else data


def atomic_write_json(path, data, *, indent=None):
    """Write JSON durably without exposing readers to a truncated file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=indent)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
