"""Fast map-menu state. Verification runs in the worker, never in the UI."""
from pathlib import Path
import re

ACTIVE_STATES = {"queued", "downloading", "building", "verifying", "installing", "waiting_wifi", "waiting_park"}
COLORS = {"installed": (85, 215, 120, 255), "pending": (255, 210, 65, 255),
          "error": (245, 100, 100, 255), "available": (255, 255, 255, 255)}


def record_matches(path, record):
  if not isinstance(record, dict) or record.get("integrity") != "ok":
    return False
  if not re.fullmatch("[0-9a-f]{64}", str(record.get("sha256", ""))):
    return False
  if any(type(record.get(key)) is not int or record[key] <= 0 for key in ("roads", "bytes", "mtime_ns")):
    return False
  path = Path(path)
  try:
    if path.is_symlink() or not path.is_file():
      return False
    stat = path.stat()
    return stat.st_size == record.get("bytes") and stat.st_mtime_ns == record.get("mtime_ns")
  except OSError:
    return False


def country_state(package_id, selected, installed, progress, directory):
  if not isinstance(package_id, str) or not re.fullmatch("[A-Z0-9][A-Z0-9_-]{0,31}", package_id):
    return "available"
  active = progress.get("id") == package_id
  state = progress.get("state") if isinstance(progress.get("state"), str) else ""
  if active and state == "error":
    return "error"
  if active and state in ACTIVE_STATES:
    return "pending"
  if record_matches(Path(directory) / (package_id + ".sqlite"), installed.get(package_id)):
    return "installed"
  return "pending" if package_id in selected else "available"
