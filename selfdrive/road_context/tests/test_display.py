from types import SimpleNamespace
import pytest
from openpilot.selfdrive.road_context.display import speed_sign, camera_candidate, speed_camera_warning


def context(**changes):
  fields = {'valid': True, 'conflict': False, 'unit': 'm/s', 'effectiveLimit': 50/3.6, 'confidence': .95, 'age': .1}
  return SimpleNamespace(timestamp=10., speed=SimpleNamespace(**(fields | changes)))


def test_sign_freshness_and_kph():
  assert speed_sign(context(), 10.2) == 50
  assert speed_sign(context(), 11.) is None
  assert speed_sign(context(), 9.) is None
  assert speed_sign(None, 10.) is None


@pytest.mark.parametrize('fields', [{'valid': False}, {'conflict': True}, {'unit': 'mph'},
                                   {'effectiveLimit': float('nan')}, {'effectiveLimit': 0},
                                   {'confidence': .2}, {'age': 31}, {'age': -1}])
def test_invalid_limit_never_draws_number(fields):
  assert speed_sign(context(**fields), 10.1) is None


@pytest.mark.parametrize('text', ['[]', 'null', '1', '{"signs": null}', '{"signs": [null, 1, "x"]}', '{'])
def test_malformed_diagnostics_cannot_crash_hud(text):
  assert camera_candidate(SimpleNamespace(timestamp=10., diagnostics=text), 10.1) is None


def test_camera_warning_nearest_fresh_and_enabled():
  far = SimpleNamespace(kind='FIXED_SPEED_CAMERA', distance=900.)
  near = SimpleNamespace(kind='RED_LIGHT_CAMERA', distance=120.)
  context = SimpleNamespace(timestamp=10., warningsEnabled=True, enforcement=[far, near])
  assert speed_camera_warning(context, 10.2) is near
  assert speed_camera_warning(context, 11.) is None
  context.warningsEnabled = False
  assert speed_camera_warning(context, 10.2) is None


@pytest.mark.parametrize('kind,distance', [
  ('UNKNOWN', 50.), ('FIXED_SPEED_CAMERA', -1.), ('FIXED_SPEED_CAMERA', 1501.),
  ('FIXED_SPEED_CAMERA', float('nan')),
])
def test_invalid_camera_warning_is_not_shown(kind, distance):
  context = SimpleNamespace(timestamp=10., warningsEnabled=True,
                            enforcement=[SimpleNamespace(kind=kind, distance=distance)])
  assert speed_camera_warning(context, 10.1) is None
