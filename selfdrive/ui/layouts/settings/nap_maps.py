"""NAP country-map selector; selected OSM extracts are built on device."""
import json
import re
from pathlib import Path

from openpilot.common.params import Params
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.list_view import button_item, text_item, toggle_item
from openpilot.system.ui.widgets.scroller_tici import Scroller
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.road_context.map_sources import INDEX_URL

def country_map_status(package_id: str, *, selected: bool, installed, statuses,
                       latest_status, configured_path: str, display_enabled: bool,
                       map_dir: str):
  """Read-only UI state based on the downloader's verified record and file."""
  if not isinstance(package_id, str) or not re.fullmatch(r"[A-Z0-9_-]{1,24}", package_id):
    return "invalid", 0, ""
  directory = Path(map_dir)
  database = directory / f"{package_id}.sqlite"
  exists = database.is_file() and not database.is_symlink()
  record = installed.get(package_id) if isinstance(installed, dict) else None
  if exists and isinstance(record, dict):
    try:
      size = database.stat().st_size
      roads = record.get("roads")
      digest = record.get("sha256", "")
      verified = (record.get("id") == package_id and record.get("path") == str(database)
                  and record.get("integrity") == "ok" and record.get("bytes") == size
                  and isinstance(roads, int) and not isinstance(roads, bool) and roads > 0
                  and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None)
      if verified:
        if selected and configured_path == str(directory) and display_enabled:
          return "active", roads, ""
        if selected and configured_path == str(directory):
          return "display_off", roads, ""
        return "verified_inactive", roads, ""
    except OSError:
      pass
    return "unverified", 0, ""
  status = statuses.get(package_id) if isinstance(statuses, dict) else None
  if not isinstance(status, dict) and isinstance(latest_status, dict) and latest_status.get("id") == package_id:
    status = latest_status
  state = status.get("state") if isinstance(status, dict) else ""
  if state in ("downloading", "building", "error"):
    return state, 0, str(status.get("message", ""))[:160] if state == "error" else ""
  if state == "complete":
    return "missing", 0, ""
  if state in ("removed", "cancelled"):
    return "not_downloaded", 0, ""
  return ("queued", 0, "") if selected else ("not_downloaded", 0, "")



class NAPMapsLayout(Widget):
  def __init__(self):
    super().__init__()
    self._params = Params()
    self._items = []
    self._build()
    self._scroller = Scroller(self._items, line_separator=True, spacing=0)

  @staticmethod
  def _json(params, key, default):
    try:
      raw = params.get(key)
      if isinstance(raw, (dict, list)):
        return raw
      if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
      return json.loads(raw) if raw else default
    except (ValueError, TypeError, UnicodeDecodeError):
      return default

  def _build(self):
    self._items = [
      button_item(lambda: tr("Back"), lambda: tr("Return"), callback=gui_app.pop_widget),
      text_item(lambda: tr("European country maps"), lambda: tr("Select packages to download")),
      toggle_item(lambda: tr("Show speed limit"),
                  description=lambda: tr("Display-only map and sign estimate; does not change vehicle speed."),
                  initial_state=self._params.get_bool("RoadContextEnabled"), callback=self._set_display,
                  enabled=ui_state.is_vehicle_stationary),
      text_item(lambda: tr("Downloads"), self._download_status),
      text_item(lambda: tr("OpenStreetMap contributors"), lambda: tr("Map data under ODbL-1.0")),
    ]
    catalog = self._json(self._params, "RoadContextMapCatalog", {})
    sources = catalog.get("sources", []) if isinstance(catalog, dict) else []
    selected = self._json(self._params, "RoadContextMapSelection", None)
    if selected is None and sources:
      source_ids = {item.get("package_id") for item in sources if isinstance(item, dict)}
      if isinstance(selected, list):
        selected = sorted({item for item in selected if isinstance(item, str) and item in source_ids})
      else:
        installed = self._json(self._params, "RoadContextMapInstalled", {})
        installed_ids = set(installed) if isinstance(installed, dict) else set()
        existing_ids = {path.stem for path in Path("/data/road-context/maps").glob("*.sqlite")
                        if path.is_file() and not path.is_symlink()}
        selected = sorted((installed_ids | existing_ids) & source_ids)
      self._params.put("RoadContextMapSelection", selected)
    if not isinstance(selected, list):
      selected = []
    selected = set(selected)
    if not sources:
      self._items.append(text_item(lambda: tr("Map list syncing"),
                                   lambda: tr("Connect to Wi-Fi while parked to load the country list from Geofabrik.")))
      self._items.append(text_item(lambda: tr("OSM map source"), lambda: INDEX_URL))
      return
    installed = self._json(self._params, "RoadContextMapInstalled", {})
    latest_status = self._json(self._params, "RoadContextMapDownloadStatus", {})
    statuses = latest_status.get("countries", {}) if isinstance(latest_status, dict) else {}
    configured_path = self._params.get("RoadContextMapPath")
    if isinstance(configured_path, bytes):
      configured_path = configured_path.decode("utf-8", "replace")
    configured_path = configured_path or ""

    for package in sources:
      package_id = package.get("package_id")
      label = package.get("name", package_id or "Map")
      countries = ", ".join(package.get("countries", []))
      state, roads, message = country_map_status(
        package_id, selected=package_id in selected, installed=installed, statuses=statuses,
        latest_status=latest_status, configured_path=configured_path,
        display_enabled=self._params.get_bool("RoadContextEnabled"), map_dir="/data/road-context/maps")
      if state == "active":
        state_text = tr("Active — verified map ({roads} road segments)").format(roads=roads)
      elif state == "display_off":
        state_text = tr("Verified map configured; display is off ({roads} road segments)").format(roads=roads)
      elif state == "verified_inactive":
        state_text = tr("Downloaded and verified, inactive ({roads} road segments)").format(roads=roads)
      elif state == "unverified":
        state_text = tr("File exists; map has not been verified")
      elif state == "downloading":
        state_text = tr("Download in progress")
      elif state == "building":
        state_text = tr("Building map database")
      elif state == "error":
        state_text = tr("Download or build failed: {message}").format(message=message or tr("Unknown error"))
      elif state == "missing":
        state_text = tr("Expected map file is missing")
      elif state == "queued":
        state_text = tr("Selected — waiting for Wi-Fi while parked")
      elif state == "invalid":
        state_text = tr("Invalid map package identifier")
      else:
        state_text = tr("Not downloaded")
      desc = f"{countries} · {tr('downloads and builds automatically on Wi-Fi while parked')} · {state_text}"
      self._items.append(toggle_item(
        label, description=desc, initial_state=package_id in selected,
        callback=lambda enabled, pid=package_id: self._set_selected(pid, enabled),
        enabled=ui_state.is_vehicle_stationary,
      ))

  def _set_selected(self, package_id, enabled):
    if not ui_state.is_vehicle_stationary():
      return
    selected = self._json(self._params, "RoadContextMapSelection", [])
    selected = set(selected) if isinstance(selected, list) else set()
    if enabled:
      selected.add(package_id)
    else:
      selected.discard(package_id)
    self._params.put("RoadContextMapSelection", sorted(selected))

  def _set_display(self, enabled):
    if ui_state.is_vehicle_stationary():
      self._params.put_bool("RoadContextEnabled", enabled)

  def _download_status(self):
    status = self._json(self._params, "RoadContextMapDownloadStatus", {})
    if not isinstance(status, dict) or not status:
      return tr("Waiting for Wi-Fi")
    state = status.get("state", "waiting")
    detail = status.get("message") or status.get("roads")
    return f"{status.get('id', '')}: {state}" + (f" · {detail}" if detail else "")

  def _render(self, rect):
    self._scroller.render(rect)

  def show_event(self):
    self._build()
    self._scroller = Scroller(self._items, line_separator=True, spacing=0)
    self._scroller.show_event()
