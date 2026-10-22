"""Headless regressions for camera evidence, freshness and the rendered MAX/sign HUD."""
import ast
import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock

from openpilot.selfdrive.road_context.display import camera_candidate, speed_sign
from openpilot.selfdrive.road_context.engine import RoadContextEngine
from openpilot.selfdrive.road_context.fusion import SpeedLimitFusion
from openpilot.selfdrive.road_context.ocr_vision import SignConsensus
from openpilot.selfdrive.road_context.tsr import SignObservation, relevance, speed_observation
from openpilot.selfdrive.road_context.types import RoadMatch

ROOT = Path(__file__).parents[2]
MATCH = RoadMatch(road_id='main', confidence=.99, direction_match=True)


def sign(timestamp=10., frame=1, **changes):
  return replace(SignObservation('LIMIT', 50, .99, timestamp, frame, (.60, .30, .70, .50), 'main', 90.), **changes)


def wire_context(value=50, timestamp=10., **changes):
  fields = dict(valid=True, conflict=False, unit='m/s', effectiveLimit=value/3.6, confidence=.99, age=.1, source='MAP')
  return SimpleNamespace(timestamp=timestamp, speed=SimpleNamespace(**(fields | changes)))


def method(path, cls_name, name, env):
  tree = ast.parse(path.read_text(encoding='utf-8'))
  cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls_name)
  node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
  exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), env)
  return env[name]


class TsrEvidenceTests(unittest.TestCase):
  def test_three_unique_frames_required(self):
    tracker = SignConsensus()
    self.assertEqual(tracker.update([sign()], 10., 1), [])
    self.assertEqual(tracker.update([sign(10.1, 1)], 10.1, 1), [])
    self.assertEqual(tracker.update([sign(10.2, 2)], 10.2, 2), [])
    self.assertEqual(len(tracker.update([sign(10.3, 3)], 10.3, 3)), 1)

  def test_gap_or_different_value_requires_new_confirmation(self):
    for change in ('gap', 'value', 'missing', 'different_sign'):
      with self.subTest(change=change):
        tracker = SignConsensus()
        tracker.update([sign()], 10., 1)
        tracker.update([sign(10.5, 2)], 10.5, 2)
        t = 12. if change == 'gap' else 11.
        observation = sign(t, 3, value_kph=80 if change == 'value' else 50,
                           bbox=(.68, .30, .78, .50) if change == 'different_sign' else (.60, .30, .70, .50))
        if change == 'missing':
          tracker.update([], 10.7, 9)
        self.assertEqual(tracker.update([observation], t, 3), [])

  def test_one_previous_sign_cannot_confirm_two_proposals(self):
    tracker = SignConsensus()
    tracker.update([sign()], 10., 1)
    tracker.update([sign(10.2, 2)], 10.2, 2)
    self.assertEqual(len(tracker.update([sign(10.4, 3), sign(10.4, 3)], 10.4, 3)), 1)

  def test_restarted_frame_numbers_cannot_reuse_evidence(self):
    tracker = SignConsensus()
    tracker.update([sign()], 10., 1)
    tracker.update([sign(10.2, 2)], 10.2, 2)
    self.assertEqual(tracker.update([sign(10.4, 1)], 10.4, 1), [])
    self.assertEqual(tracker.update([sign(10.6, 2)], 10.6, 2), [])
    self.assertEqual(len(tracker.update([sign(10.8, 3)], 10.8, 3)), 1)

  def test_invalid_clock_cannot_confirm(self):
    tracker = SignConsensus()
    tracker.update([sign()], 10., 1)
    for t in (9., 10., float('nan'), float('inf')):
      self.assertEqual(tracker.update([sign(t, 2)], t, 2), [])

  def test_camera_observation_to_display_and_expiry(self):
    fusion = SpeedLimitFusion()
    for frame, t in ((1, 10.), (2, 10.2)):
      observation = speed_observation(sign(t, frame), MATCH, 90., path_image=(.4, .9))
      result = fusion.update(t, MATCH, camera=observation, frame_id=frame)
    self.assertTrue(result.valid)
    context = wire_context(result.effective_limit*3.6, 10.2, source=result.source, confidence=result.confidence)
    self.assertEqual(speed_sign(context, 10.3), 50)
    self.assertIsNone(speed_sign(context, 11.))
    self.assertFalse(fusion.update(31., MATCH).valid)

  def test_unassociated_camera_is_not_a_posted_limit(self):
    observation = sign(road_id='', facing_heading=None)
    self.assertEqual(relevance(observation, MATCH, 90.)[1], 'UNRESOLVED_LANE_RELEVANCE')
    self.assertIsNone(speed_sign(wire_context(valid=False), 10.1))

  def test_malformed_boxes_are_rejected(self):
    for box in (None, 12, (), (.1, .2), ('x', .2, .3, .4)):
      with self.subTest(box=box):
        self.assertEqual(relevance(sign(bbox=box), MATCH, 90.)[1], 'INVALID_OBSERVATION')

  def test_stale_detection_diagnostic_is_explicit(self):
    result = RoadContextEngine().update(20., 1000., signs=[sign()])
    self.assertEqual(result['rejected_signs'][0]['reason'], 'STALE_SIGN')

  def test_bad_diagnostic_fields_do_not_crash_display(self):
    for value in ([], None, '50', True, {}):
      for field in ('confidence', 'timestamp', 'value_kph'):
        row = dict(source='OCR_CONSENSUS_RED_CIRCLE_UNVALIDATED', kind='LIMIT', value_kph=50, confidence=.99, timestamp=10.)
        row[field] = value
        context = SimpleNamespace(timestamp=10., diagnostics=json.dumps(dict(signs=[row])))
        self.assertIsNone(camera_candidate(context, 10.1))


class SpeedSignHudTests(unittest.TestCase):
  def setup_renderer(self):
    rl = Mock()
    rl.Vector2 = lambda x, y: SimpleNamespace(x=x, y=y)
    env = dict(rl=rl, time=SimpleNamespace(monotonic=lambda: 10.2), speed_sign=speed_sign,
               tr=lambda text: text, FontWeight=SimpleNamespace(BOLD=1), gui_app=Mock(),
               measure_text_cached=lambda font, text, size: SimpleNamespace(x=len(text)*size*.5, y=size))
    render = method(ROOT/'ui/onroad/road_context.py', 'RoadContextWidget', '_render', env)
    widget = SimpleNamespace(params=SimpleNamespace(get_bool=lambda key: key in ('RoadContextEnabled', 'IsMetric')),
                             sm=MagicMock(), _limit=None, _source='', _context=None)
    widget.sm.valid = {'roadContext': True}
    return rl, render, widget

  def test_dynamic_limit_unknown_and_stale_render(self):
    rl, render, widget = self.setup_renderer()
    rect = SimpleNamespace(x=280., y=50., width=188., height=188.)
    for context, expected in ((wire_context(50), '50'), (wire_context(80), '80'), (wire_context(130), '130'),
                              (wire_context(valid=False), '--'), (wire_context(timestamp=8.), '--')):
      widget.sm.__getitem__.return_value = context
      render(widget, rect)
      call = rl.draw_text_ex.call_args.args
      self.assertEqual(call[1], expected)
      self.assertEqual(call[-1], rl.BLACK)
      center = rl.draw_circle_v.call_args_list[-2].args[0]
      self.assertEqual((center.x, center.y), (374., 144.))
      # Three digits and unknown values must fit inside the white disk.
      self.assertGreaterEqual(call[2].x, rect.x + 22)
      self.assertLessEqual(call[2].x + len(expected)*call[3]*.5, rect.x + rect.width - 22)

  def test_european_sign_units_with_imperial_cluster(self):
    rl, render, widget = self.setup_renderer()
    widget.params.get_bool = lambda key: key == 'RoadContextEnabled'
    widget.sm.__getitem__.return_value = wire_context(100)
    render(widget, SimpleNamespace(x=0, y=0, width=204, height=204))
    self.assertEqual(rl.draw_text_ex.call_args.args[1], '100')

  def test_disabled_sign_does_not_render(self):
    rl, render, widget = self.setup_renderer()
    widget.params.get_bool = lambda key: False
    widget._context = wire_context()
    render(widget, SimpleNamespace(x=0, y=0, width=204, height=204))
    rl.draw_circle_v.assert_not_called()
    self.assertIsNone(widget._context)

  def test_max_and_sign_rectangles_do_not_overlap(self):
    rl = Mock()
    rl.Rectangle = lambda x, y, w, h: SimpleNamespace(x=x, y=y, width=w, height=h)
    config = SimpleNamespace(set_speed_width_metric=200, set_speed_width_imperial=172)
    state = SimpleNamespace(is_metric=True)
    draw = method(ROOT/'ui/onroad/hud_renderer.py', 'HudRenderer', '_draw_speed_limit',
                  dict(rl=rl, UI_CONFIG=config, ui_state=state))
    widget = SimpleNamespace(_road_context=Mock())
    for metric in (True, False):
      state.is_metric = metric
      draw(widget, SimpleNamespace(x=30, y=30))
      rect = widget._road_context.render.call_args.args[0]
      width = 200 if metric else 172
      max_right = 30 + 60 + (172-width)//2 + width
      self.assertGreaterEqual(rect.x-max_right, 24)
      self.assertEqual(rect.y, 80)
      self.assertEqual((rect.width, rect.height), (188, 188))
      self.assertLess(rect.x+rect.width, 960)  # Clear of the central speedometer.


if __name__ == '__main__':
  unittest.main()
