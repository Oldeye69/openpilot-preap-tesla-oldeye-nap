from dataclasses import dataclass
from openpilot.selfdrive.road_context.types import finite


@dataclass(frozen=True)
class LightingContext:
  high_beam_allowed: bool
  confidence: float
  reason: str
  timestamp: float
  age: float
  ambient: str = 'UNKNOWN'
  oncoming: bool | None = None
  lead: bool | None = None
  street_lighting: bool | None = None


class AutoHighBeamPerception:
  def evaluate(
    self,
    now,
    timestamp,
    speed,
    *,
    ambient='UNKNOWN',
    oncoming=None,
    lead=None,
    street_lighting=None,
    confidence=0.0,
    camera_available=False,
    minimum_speed=12.0,
  ):
    age = now - timestamp if finite(now, timestamp) else -1.0
    reason = 'CLEAR'
    if not camera_available or not 0 <= age <= 0.5:
      reason = 'CAMERA_UNAVAILABLE'
    elif not finite(confidence, speed) or not 0.9 <= confidence <= 1 or None in (oncoming, lead, street_lighting) or ambient == 'UNKNOWN':
      reason = 'LOW_CONFIDENCE'
    elif oncoming:
      reason = 'ONCOMING'
    elif lead:
      reason = 'LEAD_VEHICLE'
    elif street_lighting:
      reason = 'URBAN_LIGHTING'
    elif ambient != 'DARK':
      reason = 'AMBIENT_TOO_BRIGHT'
    elif speed < minimum_speed:
      reason = 'LOW_SPEED'
    return LightingContext(reason == 'CLEAR', confidence, reason, timestamp, age, ambient, oncoming, lead, street_lighting)


class AutoHighBeamStateMachine:
  """Intent only. No stalk writes, CAN imports, relay output or flash suppression."""

  def __init__(self, double_pull_window=0.5, clear_delay=2.0, timeout=0.5):
    if not finite(double_pull_window, clear_delay, timeout) or not 0.15 <= double_pull_window <= 1 or clear_delay < 0 or timeout <= 0:
      raise ValueError('Explicit gesture/timeout settings required')
    self.window, self.clear_delay, self.timeout = double_pull_window, clear_delay, timeout
    self.enabled = False
    self.state = 'OFF'
    self.last_flash = None
    self.was_flash = False
    self.clear_since = None
    self.last_time = None

  def update(self, now, context, *, flash=False, manual_high=False, connected=True):
    if not finite(now) or not connected or (self.last_time is not None and now < self.last_time):
      self.enabled, self.last_flash, self.clear_since = False, None, None
      self.state = 'OFF'
    else:
      if flash and not self.was_flash:
        if self.last_flash is not None and 0 < now - self.last_flash <= self.window:
          self.enabled = not self.enabled
          self.last_flash = None
          self.clear_since = None
        else:
          self.last_flash = now
      if not self.enabled:
        self.state = 'OFF'
      elif manual_high or flash:
        self.state = 'MANUAL_OVERRIDE'
        self.clear_since = None
      elif not context or not finite(context.timestamp) or not 0 <= now - context.timestamp <= self.timeout:
        self.state, self.clear_since = 'AUTO_LOW', None
      elif not context.high_beam_allowed:
        self.state, self.clear_since = 'AUTO_LOW', None
      else:
        self.clear_since = now if self.clear_since is None else self.clear_since
        self.state = 'AUTO_HIGH' if now - self.clear_since >= self.clear_delay else 'AUTO_READY'
    self.was_flash, self.last_time = flash, now
    requested = 'DRIVER' if manual_high or flash else ('HIGH' if self.state == 'AUTO_HIGH' else ('LOW' if self.enabled else 'NONE'))
    return {'state': self.state, 'requested_light_state': requested, 'timestamp': now, 'intent_only': True}


class SwitchedAutoHighBeam:
  """Switch AND verified driver high-beam request; logical output only.

  Actual lamp state is not the arming signal: dimming must not disarm the
  driver's persistent high-beam request. Missing/stale driver evidence is UNKNOWN.
  Legacy double-pull gestures are not consumed by this controller.
  """

  def __init__(self, clear_delay=2.0, timeout=0.5):
    if not finite(clear_delay, timeout) or clear_delay < 0 or timeout <= 0:
      raise ValueError('Invalid timing')
    self.clear_delay, self.timeout = clear_delay, timeout
    self.clear_since = self.last_time = None

  def update(self, now, context, *, enabled=False, driver_high_requested=None, driver_timestamp=None, flash=False):
    reason, state, requested = 'DISABLED', 'OFF', 'NONE'
    if self.last_time is not None and (not finite(now) or now - self.last_time > self.timeout):
      self.clear_since = None
    valid_clock = finite(now) and (self.last_time is None or now >= self.last_time)
    if flash:
      state, requested, reason = 'MANUAL_OVERRIDE', 'DRIVER', 'FLASH_TO_PASS'
    elif enabled and valid_clock:
      if type(driver_high_requested) is not bool or driver_timestamp is None or not finite(driver_timestamp) or not 0 <= now - driver_timestamp <= self.timeout:
        reason = 'DRIVER_HIGH_UNAVAILABLE'
      elif not driver_high_requested:
        reason = 'DRIVER_HIGH_OFF'
      elif not context or not finite(context.timestamp) or not 0 <= now - context.timestamp <= self.timeout:
        state, requested, reason = 'AUTO_LOW', 'LOW', 'CAMERA_UNAVAILABLE'
      elif not context.high_beam_allowed:
        state, requested, reason = 'AUTO_LOW', 'LOW', context.reason
      else:
        self.clear_since = now if self.clear_since is None else self.clear_since
        state = 'AUTO_HIGH' if now - self.clear_since >= self.clear_delay else 'AUTO_READY'
        requested, reason = ('HIGH' if state == 'AUTO_HIGH' else 'LOW'), 'CLEAR'
    if reason != 'CLEAR':
      self.clear_since = None
    self.last_time = now if finite(now) else None
    return {'state': state, 'requested_light_state': requested, 'timestamp': now, 'intent_only': True, 'inhibit_reason': reason}
