from dataclasses import asdict
from openpilot.selfdrive.road_context.types import SpeedLimitContext, finite


class SpeedLimitFusion:
  def __init__(self):
    self.road_id = ''
    self.camera = None
    self.confirmations = 0
    self.last_frame = None
    self.ended_road = None
    self.end_time = -1.0

  def reset(self):
    self.camera = None
    self.confirmations = 0
    self.last_frame = None

  def update(self, now, match, observations=(), camera=None, frame_id=None, end_limit=False,
             road_transition=False, gps_available=True, camera_available=True, upcoming=None):
    if road_transition or self.road_id != match.road_id:
      self.reset()
      self.ended_road = None
    self.road_id = match.road_id
    if end_limit:
      self.reset()
      self.ended_road, self.end_time = match.road_id, now
    if not gps_available or not camera_available or (self.camera and not 0 <= now-self.camera.timestamp <= self.camera.ttl):
      self.reset()
    if camera is not None and camera_available and gps_available and frame_id != self.last_frame:
      if not camera.rejection(now, match.road_id):
        self.confirmations = self.confirmations + 1 if self.camera and self.camera.value == camera.value else 1
        self.camera = camera
        if self.confirmations >= 2:
          self.ended_road = None
      self.last_frame = frame_id
    candidates = list(observations)
    if self.camera is not None:
      candidates.append(self.camera)
    out = SpeedLimitContext(road_id=match.road_id, road_match_confidence=match.confidence, direction_match=match.direction_match)
    scored = []
    for obs in candidates:
      out.observations.append(asdict(obs))
      reject = obs.rejection(now, match.road_id)
      if not gps_available or not finite(match.confidence) or not .7 <= match.confidence <= 1 or not match.direction_match:
        reject = 'LOCATION_UNCERTAIN'
      if obs.source == 'CAMERA' and (not camera_available or self.confirmations < 2):
        reject = 'CAMERA_UNAVAILABLE' if not camera_available else 'UNCONFIRMED'
      if self.ended_road == match.road_id and obs.source in ('MAP', 'NAVIGATION'):
        reject = 'END_LIMIT_REQUIRES_NEW_CONTEXT'
      if reject:
        out.rejected.append({'source': obs.source, 'reason': reject})
        continue
      score = obs.confidence * obs.relevance * min(obs.match_confidence, match.confidence)
      score *= max(.5, 1 - (now - obs.timestamp) / (2 * obs.ttl))
      if obs.source == 'CAMERA' and obs.limit_type in ('TEMPORARY', 'VARIABLE'):
        score = min(1.0, score + .12)
      scored.append((score, obs))
      if obs.source == 'CAMERA':
        out.camera_limit = obs.value
      if obs.source == 'MAP':
        out.map_limit = obs.value
    if not scored:
      return out
    scored.sort(key=lambda x: x[0], reverse=True)
    score, chosen = scored[0]
    conflicts = [(s, o) for s, o in scored[1:] if abs(o.value - chosen.value) > .6]
    if conflicts and score - conflicts[0][0] < .12:
      out.conflict, out.reason = True, 'SOURCE_CONFLICT'
      return out
    out.effective_limit, out.source = chosen.value, chosen.source
    out.confidence, out.age = score, now - chosen.timestamp
    out.valid, out.limit_type, out.reason = True, chosen.limit_type, 'FUSED'
    out.control_eligible = chosen.control_eligible and score >= .85 and not conflicts
    if upcoming is not None:
      next_obs, distance = upcoming
      if not next_obs.rejection(now, next_obs.road_id) and finite(distance) and distance >= 0 and match.confidence >= .85:
        out.next_limit, out.distance_to_next_limit = next_obs.value, distance
    return out
