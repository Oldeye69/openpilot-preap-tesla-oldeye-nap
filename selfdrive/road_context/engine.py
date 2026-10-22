from dataclasses import asdict
from openpilot.selfdrive.road_context.adapters import NavigationAdapter
from openpilot.selfdrive.road_context.enforcement import EnforcementContext, EnforcementPolicy
from openpilot.selfdrive.road_context.fusion import SpeedLimitFusion
from openpilot.selfdrive.road_context.lighting import AutoHighBeamPerception, SwitchedAutoHighBeam
from openpilot.selfdrive.road_context.tsr import relevance, speed_observation
from openpilot.selfdrive.road_context.types import RoadMatch


class RoadContextEngine:
  def __init__(self, maps=None, points=(), enforcement_policy=None):
    self.maps, self.points = maps, points
    self.fusion = SpeedLimitFusion()
    self.enforcement = EnforcementContext()
    self.enforcement_policy = enforcement_policy or EnforcementPolicy()
    self.lights = AutoHighBeamPerception()
    self.ahb = SwitchedAutoHighBeam()
    self.previous_road = ''
    self.previous_route = ()
    self.previous_policy = None
    self.previous_effective = None
    self.previous_ahb = 'OFF'

  def update(
    self,
    now,
    epoch,
    position=None,
    signs=(),
    lighting=None,
    route=(),
    navigation=None,
    path_image=None,
    flash=False,
    manual_high=False,
    camera_timestamp=None,
    auto_high_beam=False,
    driver_high_requested=None,
    driver_timestamp=None,
  ):
    match = self.maps.match(position, now, route) if self.maps else RoadMatch()
    observations = []
    upcoming = None
    road = None
    if self.maps and match.road_id:
      observations.append(self.maps.observation(match, now, epoch))
      upcoming = self.maps.upcoming(match, now, epoch, route)
      road = self.maps.get(match.road_id)
    nav = NavigationAdapter().observation(navigation, now, match)
    if nav is not None:
      observations.append(nav)
    accepted, rejected, ends = [], [], False
    sign_details = []
    heading = position.heading if position else 0
    for sign in signs:
      score, reason = relevance(sign, match, heading, path_image=path_image, route=route)
      sign_details.append(asdict(sign) | {'relevance': score, 'relevance_reason': reason, 'ttl': 20.0})
      if score < 0.75 or sign.confidence < 0.7 or not 0 <= now - sign.timestamp <= 0.5:
        rejected.append({'frame': sign.frame_id, 'kind': sign.kind, 'bbox': sign.bbox, 'reason': reason})
      elif sign.kind in ('END_LIMIT', 'END_ALL', 'URBAN_ENTRY', 'URBAN_EXIT', 'MOTORWAY_ENTRY', 'MOTORWAY_EXIT', 'EXPRESSWAY_ENTRY', 'EXPRESSWAY_EXIT'):
        ends = True  # invalidate old numeric state; never guess a new country limit from an unqualified context sign
      else:
        observation = speed_observation(sign, match, heading, path_image=path_image, route=route)
        if observation is not None:
          accepted.append((observation, sign.frame_id))
    camera, frame_id = max(accepted, key=lambda item: item[0].confidence, default=(None, None))
    light_input = lighting or {'camera_available': False}
    speed = self.fusion.update(
      now,
      match,
      observations,
      camera,
      frame_id=frame_id,
      end_limit=ends,
      road_transition=self.previous_road != match.road_id or self.previous_route != tuple(route),
      gps_available=bool(position and position.fresh(now)),
      camera_available=light_input.get('camera_available', False),
      upcoming=upcoming,
    )
    self.enforcement_policy.transition(match.country if match.confidence >= 0.85 else '')
    warnings = self.enforcement.warnings(self.points, self.enforcement_policy, match, position, road, now, epoch)
    lights = self.lights.evaluate(now, camera_timestamp if camera_timestamp is not None else now, position.speed if position else 0.0, **light_input)
    ahb = self.ahb.update(now, lights, flash=flash, enabled=auto_high_beam, driver_high_requested=driver_high_requested, driver_timestamp=driver_timestamp)
    transitions = []
    policy_state = (self.enforcement_policy.country, self.enforcement_policy.enabled, self.enforcement_policy.prompt)
    if policy_state != self.previous_policy:
      transitions.append({'type': 'COUNTRY_POLICY', 'from': self.previous_policy, 'to': policy_state})
    self.previous_policy, self.previous_route = policy_state, tuple(route)
    if speed.effective_limit != self.previous_effective:
      transitions.append({'type': 'LIMIT', 'from': self.previous_effective, 'to': speed.effective_limit, 'reason': speed.reason})
    if ahb['state'] != self.previous_ahb:
      transitions.append({'type': 'AHB', 'from': self.previous_ahb, 'to': ahb['state'], 'reason': lights.reason})
    self.previous_road, self.previous_effective, self.previous_ahb = match.road_id, speed.effective_limit, ahb['state']
    return {
      'schema_version': 1,
      'timestamp': now,
      'speed': asdict(speed),
      'match': asdict(match),
      'signs': sign_details,
      'rejected_signs': rejected,
      'enforcement': warnings,
      'country_policy': {
        'country': self.enforcement_policy.country,
        'state': self.enforcement_policy.current['state'],
        'prompt': self.enforcement_policy.prompt,
        'enabled': self.enforcement_policy.enabled,
        'disabled_reason': self.enforcement_policy.disabled_reason,
      },
      'lighting': asdict(lights),
      'ahb': ahb,
      'transitions': transitions,
    }
