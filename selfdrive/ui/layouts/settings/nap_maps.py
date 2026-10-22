"""NAP Maps: country selection and live installation colours."""
import json
import math
import time
from pathlib import Path
import pyray as rl

from openpilot.common.params import Params
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.list_view import button_item, text_item, toggle_item
from openpilot.system.ui.widgets.scroller_tici import Scroller
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.selfdrive.road_context.map_params import load_json
from openpilot.selfdrive.road_context.map_sources import MapSource, ID_RE, COUNTRY_RE, _source_url
from openpilot.selfdrive.road_context.map_state import country_state, COLORS

MAP_DIR = Path("/data/road-context/maps")
BUNDLED_CATALOG = Path(__file__).parents[3] / "road_context/data/europe_sources.json"
STATES = {"downloading": "Downloading", "building": "Building database", "verifying": "Verifying",
          "installing": "Installing", "complete": "Installed", "removed": "Removed", "cancelled": "Paused",
          "waiting_wifi": "Waiting for Wi-Fi", "waiting_park": "Waiting until parked", "error": "Download error"}


def can_select_maps():
  if ui_state.is_offroad():
    return True
  sm = ui_state.sm
  speed = sm["carState"].vEgo
  return sm.valid["carState"] and sm.alive["carState"] and math.isfinite(speed) and abs(speed) < .01


class MapLegend(Widget):
  def __init__(self):
    super().__init__()
    self.set_rect(rl.Rectangle(0, 0, 0, 75))

  def set_parent_rect(self, rect):
    super().set_parent_rect(rect)
    self._rect.width = rect.width

  def _render(self, rect):
    font = gui_app.font(FontWeight.MEDIUM)
    rl.draw_text_ex(font, tr("Green: installed"), rl.Vector2(rect.x + 20, rect.y + 15), 34, 0, rl.Color(*COLORS["installed"]))
    rl.draw_text_ex(font, tr("Yellow: queued or installing"), rl.Vector2(rect.x + rect.width / 2, rect.y + 15), 34, 0, rl.Color(*COLORS["pending"]))


class NAPMapsLayout(Widget):
  def __init__(self):
    super().__init__()
    self._params = Params()
    self._items, self._rows, self._sources = [], {}, []
    self._selected, self._installed, self._progress = set(), {}, {}
    self._signature = None
    self._next_refresh = 0.
    self._refresh(force=True)

  def _catalog_sources(self):
    catalog = load_json(self._params, "RoadContextMapCatalog", {})
    if not isinstance(catalog, dict) or not isinstance(catalog.get("sources"), list) or not catalog["sources"]:
      try:
        catalog = json.loads(BUNDLED_CATALOG.read_text(encoding="utf-8"))
      except (OSError, ValueError):
        catalog = {}
    sources = []
    for item in catalog.get("sources", [])[:500]:
      try:
        pid, name, countries = item["package_id"], item["name"], tuple(item["countries"])
        if not ID_RE.fullmatch(pid) or not countries or not all(COUNTRY_RE.fullmatch(c) for c in countries):
          continue
        sources.append(MapSource(pid, str(name)[:120], _source_url(item["url"]), countries))
      except (KeyError, TypeError, ValueError):
        continue
    return sources

  def _selection(self, sources):
    selected = load_json(self._params, "RoadContextMapSelection", None)
    ids = {s.package_id for s in sources}
    if not isinstance(selected, list):
      selected = [pid for pid in ids if (MAP_DIR / (pid + ".sqlite")).is_file()]
    return {pid for pid in selected if isinstance(pid, str) and pid in ids}

  def _refresh(self, force=False):
    now = time.monotonic()
    if not force and now < self._next_refresh:
      return
    self._next_refresh = now + .5
    sources = self._catalog_sources()
    self._selected = self._selection(sources)
    installed = load_json(self._params, "RoadContextMapInstalled", {})
    progress = load_json(self._params, "RoadContextMapDownloadStatus", {})
    self._installed = installed if isinstance(installed, dict) else {}
    self._progress = progress if isinstance(progress, dict) else {}
    signature = tuple((s.package_id, s.name, s.countries) for s in sources)
    if force or signature != self._signature:
      self._signature, self._sources = signature, sources
      self._build()
    for pid, row in self._rows.items():
      row.action_item.set_state(pid in self._selected)

  def _state(self, pid):
    return country_state(pid, self._selected, self._installed, self._progress, MAP_DIR)

  def _colour(self, pid):
    return rl.Color(*COLORS[self._state(pid)])

  def _detail(self, source):
    state = self._state(source.package_id)
    status = {"installed": "Installed", "pending": "Queued for Wi-Fi download", "error": "Download error", "available": "Not installed"}[state]
    return ", ".join(source.countries) + " · " + tr(status) + ". " + tr("Selection downloads on Wi-Fi while parked. Deselecting removes the map.")

  def _build(self):
    self._rows = {}
    self._items = [button_item(lambda: tr("Maps"), lambda: tr("Back"), callback=gui_app.pop_widget),
                   MapLegend(), text_item(lambda: tr("Downloads"), self._download_status)]
    for source in sorted(self._sources, key=lambda s: tr(s.name).casefold()):
      pid = source.package_id
      item = toggle_item(lambda s=source: tr(s.name), description=lambda s=source: self._detail(s),
                         initial_state=pid in self._selected, enabled=can_select_maps,
                         callback=lambda enabled, p=pid: self._set_selected(p, enabled))
      item.set_title_color(lambda p=pid: self._colour(p))
      self._rows[pid] = item
      self._items.append(item)
    self._items.append(text_item(lambda: tr("OpenStreetMap contributors"), lambda: "ODbL-1.0"))
    self._scroller = Scroller(self._items, line_separator=True, spacing=0)

  def _set_selected(self, package_id, enabled):
    if not can_select_maps() or package_id not in self._rows:
      return
    selected = self._selection(self._sources)
    selected.add(package_id) if enabled else selected.discard(package_id)
    self._params.put("RoadContextMapSelection", sorted(selected))
    self._refresh(force=False)
    self._selected = selected

  def _download_status(self):
    state = self._progress.get("state", "waiting_wifi")
    state = state if isinstance(state, str) else "waiting_wifi"
    title = tr(STATES.get(state, "Waiting for Wi-Fi"))
    pid = self._progress.get("id", "")
    pid = pid if isinstance(pid, str) else ""
    if pid:
      title = pid + ": " + title
    received, total = self._progress.get("received"), self._progress.get("bytes_total")
    if type(received) is int and type(total) is int and total > 0:
      title += f" · {min(100, max(0, received * 100 // total))}%"
    return title

  def _render(self, rect):
    self._refresh()
    self._scroller.render(rect)

  def show_event(self):
    self._refresh()
    self._scroller.show_event()
