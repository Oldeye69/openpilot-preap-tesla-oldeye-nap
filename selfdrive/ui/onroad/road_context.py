"""Read-only road speed-limit display. It is not a vehicle-control input."""
import time

import pyray as rl
from cereal import messaging
from openpilot.common.params import Params
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.multilang import tr, tr_noop
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget
from openpilot.selfdrive.road_context.display import speed_sign, speed_camera_warning

if False:
  tr_noop("Speed limit unavailable")
  tr_noop("Source: {source}")
  tr_noop("km/h")
  tr_noop("Speed camera ahead: {distance} m")
  tr_noop("Average speed zone: {distance} m")
  tr_noop("Average speed zone ends in {distance} m")
  tr_noop("Red-light camera ahead: {distance} m")
  tr_noop("Enforcement zone ahead: {distance} m")


class RoadContextWidget(Widget):
  def __init__(self):
    super().__init__()
    self.params = Params()
    self.sm = None
    self._limit = None
    self._source = ""
    self._context = None

  def _render(self, rect):
    show_limit = self.params.get_bool("RoadContextEnabled")
    show_camera_warnings = self.params.get_bool("SpeedCameraWarnings")
    if not show_limit and not show_camera_warnings:
      self._limit = None
      self._source = ""
      self._context = None
      return
    if self.sm is None:
      self.sm = messaging.SubMaster(["roadContext"])
    self.sm.update(0)
    context = self.sm["roadContext"] if self.sm.valid["roadContext"] else None
    self._context = context
    now = time.monotonic()
    self._limit = speed_sign(context, now)
    self._source = tr(context.speed.source) if self._limit is not None else ""
    if not show_limit:
      return
    center = rl.Vector2(rect.x + rect.width / 2, rect.y + rect.height / 2)
    radius = min(rect.width, rect.height) / 2
    border = rl.Color(220, 35, 40, 255) if self._limit is not None else rl.Color(120, 120, 120, 255)
    rl.draw_circle_v(center, radius, border)
    rl.draw_circle_v(center, radius - 16, rl.WHITE)
    font = gui_app.font(FontWeight.BOLD)
    # European road signs stay in km/h even when the cluster uses mph.
    value = "--" if self._limit is None else str(self._limit)
    text_size = 76
    measured = measure_text_cached(font, value, text_size)
    rl.draw_text_ex(font, value, rl.Vector2(center.x - measured.x / 2, center.y - measured.y / 2), text_size, 0, rl.BLACK)

  def render_speed_camera_warning(self, rect, standard_alert_present):
    """Show an advisory only when there is no higher-priority system alert."""
    if standard_alert_present:
      return
    warning = speed_camera_warning(self._context, time.monotonic())
    if warning is None:
      return

    distance = max(0, round(warning.distance))
    if warning.kind == "AVERAGE_SPEED_END":
      text = tr("Average speed zone ends in {distance} m").format(distance=distance)
    elif warning.kind.startswith("AVERAGE_SPEED"):
      text = tr("Average speed zone: {distance} m").format(distance=distance)
    elif warning.kind == "RED_LIGHT_CAMERA":
      text = tr("Red-light camera ahead: {distance} m").format(distance=distance)
    elif warning.kind == "DANGER_ZONE":
      text = tr("Enforcement zone ahead: {distance} m").format(distance=distance)
    else:
      text = tr("Speed camera ahead: {distance} m").format(distance=distance)

    height = 100
    banner = rl.Rectangle(rect.x + 40, rect.y + rect.height - height - 24, rect.width - 80, height)
    rl.draw_rectangle_rounded(banner, .12, 8, rl.Color(105, 66, 0, 235))
    font = gui_app.font(FontWeight.SEMI_BOLD)
    text_size = 44
    measured = measure_text_cached(font, text, text_size)
    text_x = banner.x + max(24, (banner.width - measured.x) / 2)
    text_y = banner.y + (height - measured.y) / 2
    rl.draw_text_ex(font, text, rl.Vector2(text_x, text_y), text_size, 0, rl.WHITE)

  def user_interacting(self):
    return False
