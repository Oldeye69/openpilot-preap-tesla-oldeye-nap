import ast
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).parents[1] / "onroad"
SPEC = importlib.util.spec_from_file_location("night_mode", ROOT / "night_mode.py")
night_mode = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(night_mode)
NightModeGesture = night_mode.NightModeGesture


class NightModeTests(unittest.TestCase):
  def tap(self, gesture, t, x=100, y=100):
    gesture.press(t, x, y)
    gesture.release(t + 0.05, x, y)

  def test_two_taps_toggle_on_and_off(self):
    gesture = NightModeGesture()
    self.tap(gesture, 0.0)
    self.tap(gesture, 0.2)
    self.assertTrue(gesture.enabled)
    self.assertFalse(gesture.flush(1.0))

    self.tap(gesture, 2.0)
    self.tap(gesture, 2.2)
    self.assertFalse(gesture.enabled)
    self.assertFalse(gesture.flush(3.0))

  def test_single_tap_is_deferred_for_normal_click_only_in_day_mode(self):
    gesture = NightModeGesture()
    self.tap(gesture, 0.0)
    self.assertFalse(gesture.flush(0.2))
    self.assertTrue(gesture.flush(0.5))
    self.assertFalse(gesture.flush(1.0))

    gesture.enabled = True
    self.tap(gesture, 2.0)
    self.assertFalse(gesture.flush(2.5))
    self.assertTrue(gesture.enabled)

  def test_drag_and_long_press_do_not_toggle(self):
    gesture = NightModeGesture()
    gesture.press(0.0, 0, 0)
    gesture.release(0.1, 200, 200)
    self.assertIsNone(gesture.pending)
    gesture.press(1.0, 0, 0)
    gesture.release(1.5, 0, 0)
    self.assertIsNone(gesture.pending)
    self.assertFalse(gesture.enabled)

  def test_reset_clears_mode_and_partial_gesture(self):
    gesture = NightModeGesture()
    gesture.enabled = True
    self.tap(gesture, 0.0)
    gesture.reset()
    self.assertFalse(gesture.enabled)
    self.assertIsNone(gesture.pending)
    self.assertIsNone(gesture.down)

  def test_night_render_skips_camera_and_keeps_selected_feedback(self):
    tree = ast.parse((ROOT / "augmented_road_view.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "AugmentedRoadView")
    render = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_render")
    env = {
      "time": SimpleNamespace(monotonic=lambda: 1.0),
      "ui_state": SimpleNamespace(started=True, sm=object()),
      "rl": Mock(),
    }
    exec(compile(ast.Module(body=[render], type_ignores=[]), "augmented_road_view._render", "exec"), env)
    obj = SimpleNamespace(
      night_mode=True,
      _click_callback=Mock(),
      _hud_renderer=Mock(),
      alert_renderer=Mock(),
      driver_state_renderer=Mock(),
      _switch_stream_if_needed=Mock(),
    )
    rect = object()
    env["_render"](obj, rect)
    env["rl"].draw_rectangle_rec.assert_called_once_with(rect, env["rl"].BLACK)
    obj._hud_renderer.render.assert_called_once_with(rect)
    obj.alert_renderer.render.assert_called_once_with(rect)
    obj._hud_renderer.render_speed_camera_warning.assert_called_once()
    obj.driver_state_renderer.render.assert_called_once_with(rect)
    obj._switch_stream_if_needed.assert_not_called()

  def test_night_hud_keeps_set_speed_and_speed_limit_only(self):
    tree = ast.parse((ROOT / "hud_renderer.py").read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "HudRenderer")
    render = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_render")
    env = {"rl": Mock(), "UI_CONFIG": SimpleNamespace(header_height=300), "COLORS": SimpleNamespace()}
    exec(compile(ast.Module(body=[render], type_ignores=[]), "hud_renderer._render", "exec"), env)
    obj = SimpleNamespace(
      night_mode=True,
      _draw_set_speed=Mock(),
      _draw_speed_limit=Mock(),
      _road_context=Mock(),
      _draw_current_speed=Mock(),
      _draw_header=Mock(),
    )
    rect = SimpleNamespace(x=0, y=0, width=1000, height=700)
    env["_render"](obj, rect)
    obj._draw_set_speed.assert_called_once_with(rect)
    obj._draw_speed_limit.assert_called_once_with(rect)
    obj._draw_current_speed.assert_not_called()
    obj._draw_header.assert_not_called()


if __name__ == "__main__":
  unittest.main()
