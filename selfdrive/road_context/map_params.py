"""Typed JSON helpers for map-related Params keys."""
from __future__ import annotations

import json
import time


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
  payload = {"id": package_id, "state": state, "updated_at": time.time(), **extra}
  params.put("RoadContextMapDownloadStatus", payload)
