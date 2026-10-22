"""Exercise the native Widget event dispatcher without opening another display."""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

@unittest.skipIf(sys.platform == "win32", "Native UI integration runs on the comma")
class NativeNightModeTests(unittest.TestCase):
  def setUp(self):
    import pyray as rl
    from openpilot.system.ui.widgets import Widget
    from openpilot.selfdrive.ui.onroad import augmented_road_view as view
    from openpilot.selfdrive.ui.onroad.night_mode import NightModeGesture
    self.rl, self.module = rl, view
    class HeadlessView(view.AugmentedRoadView):
      def __init__(self):
        Widget.__init__(self)
        self.night_gesture = NightModeGesture()
        self._night_action_touch = False
        self.button_pressed = False
        self._hud_renderer = SimpleNamespace(user_interacting=lambda: self.button_pressed)
        self.set_click_callback(Mock())
        self.set_rect(rl.Rectangle(0, 0, 2160, 1080))
      def close(self): pass
    self.view = HeadlessView()
    self.started = patch.object(view, "ui_state", SimpleNamespace(started=True))
    self.started.start(); self.addCleanup(self.started.stop)

  def event(self, t, pressed, x=500, y=500, slot=0):
    return SimpleNamespace(t=t, pos=self.rl.Vector2(x,y), slot=slot,
                           left_pressed=pressed, left_released=not pressed)

  def dispatch(self, events, now):
    with patch.object(self.module.gui_app, "_mouse_events", events), patch.object(self.module.time, "monotonic", return_value=now):
      self.view._process_mouse_events()

  def double_tap(self, start=0., slot=0):
    self.dispatch([self.event(start,True,slot=slot),self.event(start+.05,False,slot=slot),
                   self.event(start+.2,True,slot=slot),self.event(start+.25,False,slot=slot)],start+.3)

  def test_real_dispatch_toggles_both_ways_without_sidebar_click(self):
    self.double_tap(); self.assertTrue(self.view.night_mode)
    self.double_tap(2.); self.assertFalse(self.view.night_mode)
    self.view._click_callback.assert_not_called()

  def test_real_dispatch_defers_normal_single_click(self):
    self.dispatch([self.event(0,True),self.event(.05,False)],.1)
    self.view._click_callback.assert_not_called()
    self.dispatch([],.6); self.view._click_callback.assert_called_once()

  def test_hidden_controls_and_secondary_touches_cannot_toggle(self):
    self.view.button_pressed=True; self.double_tap()
    self.assertFalse(self.view.night_mode)
    self.view.button_pressed=False; self.double_tap(2.,slot=1)
    self.assertFalse(self.view.night_mode)
    self.view._click_callback.assert_not_called()

  def test_hiding_view_resets_night_and_partial_gesture(self):
    self.double_tap(); self.view.hide_event()
    self.assertFalse(self.view.night_mode)
    self.assertIsNone(self.view.night_gesture.pending)

  def test_night_layout_hides_existing_sidebar_and_uses_full_screen(self):
    from openpilot.selfdrive.ui.layouts.main import MainLayout,MainState
    sidebar=SimpleNamespace(is_visible=True,render=Mock())
    sidebar.set_visible=lambda visible:setattr(sidebar,'is_visible',visible)
    onroad=SimpleNamespace(night_mode=True,render=Mock())
    full=object()
    obj=SimpleNamespace(_current_mode=MainState.ONROAD,_layouts={MainState.ONROAD:onroad},
                        _sidebar=sidebar,_sidebar_rect=object(),_content_rect=object(),_rect=full)
    MainLayout._render_main_content(obj)
    sidebar.render.assert_not_called();onroad.render.assert_called_once_with(full)

if __name__ == '__main__': unittest.main()
