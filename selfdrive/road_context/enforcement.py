"""Advisory only. Deliberately no speed-policy/controller imports."""
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from openpilot.selfdrive.road_context.maps import angle, project
from openpilot.selfdrive.road_context.types import finite

TYPES = {'FIXED_SPEED_CAMERA', 'MOBILE_CAMERA_ZONE', 'RED_LIGHT_CAMERA', 'RED_LIGHT_SPEED_CAMERA',
         'AVERAGE_SPEED_START', 'AVERAGE_SPEED_END', 'AVERAGE_SPEED_ZONE', 'DANGER_ZONE', 'UNKNOWN'}


@dataclass(frozen=True)
class EnforcementPoint:
  identity: str
  latitude: float
  longitude: float
  road_id: str
  direction: float | None
  kind: str
  country: str
  source: str
  version: str
  updated_epoch: float
  confidence: float = .8
  limit_mps: float | None = None
  zone_end_along: float | None = None
  provider_allowed: bool = False
  provider_mode: str = 'OFF'
  layer: int | None = None


class EnforcementPolicy:
  def __init__(self, path=None, *, enabled=False, disabled_reason='MANUAL'):
    self.database = json.loads(Path(path or Path(__file__).parent / 'data/enforcement_policy.json').read_text())
    self.enabled, self.disabled_reason = enabled, disabled_reason
    self.country, self.prompt, self.ack_country = '', None, ''
    self.current = self.database['default']

  def transition(self, country, reviewed_date=None):
    today = reviewed_date or date.today()
    age = (today - date.fromisoformat(self.database['reviewed'])).days
    policy = self.database['countries'].get(country, self.database['default'])
    if not 0 <= age <= self.database['max_review_age_days']:
      policy = self.database['default']
    if country != self.country:
      self.country, self.prompt, self.ack_country = country, None, ''
    self.current = policy
    if self.enabled and policy['state'] != 'ALLOWED' and self.ack_country != country:
      self.prompt = 'DISABLE_OR_KEEP'
    elif not self.enabled and self.disabled_reason == 'COUNTRY_PROMPT' and policy['state'] == 'ALLOWED':
      self.prompt = 'ENABLE_OR_KEEP_OFF'
    return self.prompt

  def choose(self, action):
    if action == 'DISABLE' and self.prompt == 'DISABLE_OR_KEEP':
      self.enabled, self.disabled_reason = False, 'COUNTRY_PROMPT'
    elif action == 'KEEP_ENABLED' and self.prompt == 'DISABLE_OR_KEEP':
      self.ack_country = self.country
    elif action == 'ENABLE' and self.prompt == 'ENABLE_OR_KEEP_OFF':
      self.enabled, self.disabled_reason = True, ''
    elif action == 'KEEP_OFF' and self.prompt == 'ENABLE_OR_KEEP_OFF':
      self.disabled_reason = 'MANUAL'
    else:
      return False
    self.prompt = None
    return True

  def manual(self, enabled):
    self.enabled, self.disabled_reason, self.prompt = enabled, '' if enabled else 'MANUAL', None
    self.ack_country = ''

  def permits(self, point):
    if not self.enabled or point.country != self.country or not point.provider_allowed:
      return False
    # Country law metadata is advisory by explicit project direction. Provider terms remain mandatory.
    if point.provider_mode not in ('PRECISE', 'DANGER_ZONE_ONLY'):
      return False
    if point.provider_mode == 'DANGER_ZONE_ONLY':
      return point.kind == 'DANGER_ZONE'  # never synthesize zones from forbidden precise points
    return True


class EnforcementContext:
  def warnings(self, points, policy, match, position, road, now, epoch):
    if not position or not position.fresh(now) or match.confidence < .85 or not match.direction_match or road is None:
      return []
    out = []
    for point in points:
      if (point.kind not in TYPES or not policy.permits(point) or point.road_id != match.road_id or point.direction is None
          or not finite(point.latitude, point.longitude, point.direction, point.updated_epoch, point.confidence, epoch)
          or not 0 <= epoch-point.updated_epoch <= 90*86400 or point.confidence < .75
          or angle(point.direction, position.heading) > 45):
        continue
      if point.layer is not None and point.layer != int(road.tags.get('layer', '0')):
        continue
      lateral, along, length, _ = project((point.latitude, point.longitude), road.geometry)
      along = along if match.forward else length-along
      ahead = along - match.along
      active_zone = (point.kind == 'AVERAGE_SPEED_ZONE' and point.zone_end_along is not None
                     and along <= match.along <= point.zone_end_along)
      if lateral > 15 or (not active_zone and not 0 <= ahead <= 1500):
        continue
      remaining = point.zone_end_along-match.along if active_zone else None
      out.append({'id': point.identity, 'type': point.kind, 'distance': max(0., ahead), 'remaining': remaining,
                  'eta': ahead/position.speed if position.speed > .5 and ahead >= 0 else None,
                  'limit': point.limit_mps, 'source': point.source, 'confidence': point.confidence,
                  'dataset_age': epoch-point.updated_epoch, 'version': point.version, 'advisory_only': True})
    return sorted(out, key=lambda item: item['distance'])[:3]
