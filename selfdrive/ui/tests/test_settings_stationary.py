from types import SimpleNamespace

from openpilot.selfdrive.ui.ui_state import vehicle_is_stationary


class MockSubMaster:
  def __init__(self, *, speed=0.0, standstill=True, can_valid=True, alive=True, valid=True, frame=100, recv_frame=100):
    self.frame = frame
    self.recv_frame = {"carState": recv_frame}
    self.alive = {"carState": alive}
    self.valid = {"carState": valid}
    self.car_state = SimpleNamespace(canValid=can_valid, standstill=standstill, vEgo=speed)

  def __getitem__(self, key):
    assert key == "carState"
    return self.car_state


def test_offroad_is_configurable_without_live_car_state():
  assert vehicle_is_stationary(MockSubMaster(alive=False, valid=False), started=False)


def test_onroad_stationary_requires_fresh_valid_zero_speed():
  assert vehicle_is_stationary(MockSubMaster(), started=True)
  assert vehicle_is_stationary(MockSubMaster(standstill=False), started=True)
  assert not vehicle_is_stationary(MockSubMaster(speed=0.5, standstill=False), started=True)
  assert not vehicle_is_stationary(MockSubMaster(can_valid=False), started=True)
  assert not vehicle_is_stationary(MockSubMaster(valid=False), started=True)
  assert not vehicle_is_stationary(MockSubMaster(frame=100, recv_frame=0), started=True)
