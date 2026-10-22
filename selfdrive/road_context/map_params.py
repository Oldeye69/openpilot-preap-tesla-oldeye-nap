"""Typed JSON helpers for map-related Params keys."""
from __future__ import annotations

import json
import time
import re
from pathlib import Path


def load_json(params, key: str, default):
  try:
    value = params.get(key)
    if isinstance(value, (dict, list)):
      return value
    if isinstance(value, bytes):
      value = value.decode("utf-8", "replace")
    return json.loads(value) if value else default
  except (ValueError, TypeError, UnicodeDecodeError):
    return default


def put_if_changed(params, key: str, value):
  current = params.get(key)
  if current != value:
    params.put(key, value)


def set_download_status(params, package_id: str, state: str, **extra):
  payload = load_json(params, "RoadContextMapDownloadStatus", {})
  countries = payload.get("countries", {}) if isinstance(payload, dict) else {}
  if not isinstance(countries, dict):
    countries = {}
  countries[package_id] = {"id": package_id, "state": state, "updated_at": time.time(), **extra}
  if len(countries) > 500:
    countries = dict(sorted(countries.items(), key=lambda item: float(item[1].get("updated_at", 0)))[-500:])
  payload = {"id": package_id, "state": state, "updated_at": time.time(), **extra, "countries": countries}
  params.put("RoadContextMapDownloadStatus", payload)


def country_map_status(package_id: str, *, selected: bool, installed, statuses,
                       latest_status, configured_path: str, display_enabled: bool,
                       map_dir: str):
  """Return UI state only when the downloader's verified record matches the on-disk file."""
  if not re.fullmatch(r"[A-Z0-9_-]{1,24}", package_id):
    return "invalid", 0, ""
  directory = Path(map_dir)
  database = directory / f"{package_id}.sqlite"
  exists = database.is_file() and not database.is_symlink()
  record = installed.get(package_id) if isinstance(installed, dict) else None
  verified = False
  roads = 0
  if exists and isinstance(record, dict):
    try:
      stat = database.stat()
      recorded_bytes = record.get("bytes")
      recorded_roads = record.get("roads")
      digest = record.get("sha256", "")
      verified = (record.get("id") == package_id and record.get("path") == str(database)
                  and record.get("integrity") == "ok"
                  and isinstance(recorded_bytes, int) and not isinstance(recorded_bytes, bool)
                  and recorded_bytes == stat.st_size
                  and isinstance(recorded_roads, int) and not isinstance(recorded_roads, bool)
                  and recorded_roads > 0 and isinstance(digest, str)
                  and re.fullmatch(r"[0-9a-f]{64}", digest) is not None)
      roads = recorded_roads if verified else 0
    except OSError:
      verified = False
  if verified:
    if selected and configured_path == str(directory) and display_enabled:
      return "active", roads, ""
    if selected and configured_path == str(directory):
      return "display_off", roads, ""
    return "verified_inactive", roads, ""
  if exists:
    return "unverified", 0, ""

  status = statuses.get(package_id) if isinstance(statuses, dict) else None
  if not isinstance(status, dict) and isinstance(latest_status, dict) and latest_status.get("id") == package_id:
    status = latest_status
  state = status.get("state") if isinstance(status, dict) else ""
  if state == "downloading":
    return "downloading", 0, ""
  if state == "building":
    return "building", 0, ""
  if state == "error":
    return "error", 0, str(status.get("message", ""))[:160]
  if state == "complete":
    return "missing", 0, ""
  if state in ("removed", "cancelled"):
    return "not_downloaded", 0, ""
  if selected:
    return "queued", 0, ""
  return "not_downloaded", 0, ""
