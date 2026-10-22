"""RoadContext uses SI units and monotonic observation timestamps, never CAN commands."""
import math
from dataclasses import dataclass, field


def finite(*values):
  return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)


@dataclass(frozen=True)
class Position:
  latitude: float
  longitude: float
  heading: float
  speed: float
  timestamp: float
  accuracy: float = 10.0
  layer: int | None = None

  def fresh(self, now):
    return (finite(self.latitude, self.longitude, self.heading, self.speed, self.timestamp, self.accuracy, now)
            and -90 <= self.latitude <= 90 and -180 <= self.longitude <= 180 and 0 <= self.accuracy <= 30
            and 0 <= now - self.timestamp <= 2 and self.speed >= 0)


@dataclass(frozen=True)
class RoadMatch:
  road_id: str = ''
  confidence: float = 0.0
  direction_match: bool = False
  forward: bool = True
  road_class: str = ''
  country: str = ''
  along: float = 0.0
  length: float = 0.0
  distance: float = math.inf
  reason: str = 'NO_MAP'


@dataclass(frozen=True)
class LimitObservation:
  source: str
  value: float | None
  timestamp: float
  confidence: float
  road_id: str
  ttl: float = 30.0
  relevance: float = 1.0
  direction_match: bool = False
  match_confidence: float = 0.0
  limit_type: str = 'POSTED'
  dataset_age: float = 0.0
  max_dataset_age: float = 90 * 86400.0
  version: str = ''
  control_eligible: bool = False
  reason: str = ''

  def rejection(self, now, road_id):
    if not finite(now, self.timestamp, self.confidence, self.ttl, self.relevance, self.match_confidence, self.dataset_age, self.max_dataset_age):
      return 'NONFINITE'
    if self.ttl <= 0 or self.max_dataset_age < 0:
      return 'INVALID_EXPIRY'
    if self.value is None or not finite(self.value) or not 0 < self.value <= 70:
      return 'UNKNOWN_LIMIT'
    if self.source not in ('MAP', 'NAVIGATION', 'CAMERA', 'DYNAMIC_MAP'):
      return 'UNKNOWN_SOURCE'
    if not 0 <= now - self.timestamp <= self.ttl or not 0 <= self.dataset_age <= self.max_dataset_age:
      return 'STALE'
    if not road_id or road_id != self.road_id:
      return 'ROAD_MISMATCH'
    if not self.direction_match:
      return 'DIRECTION'
    if not .0 <= self.confidence <= 1 or not .0 <= self.relevance <= 1 or not .0 <= self.match_confidence <= 1:
      return 'INVALID_CONFIDENCE'
    if self.confidence < .7 or self.relevance < .75 or self.match_confidence < .7:
      return 'LOW_CONFIDENCE'
    return ''


@dataclass
class SpeedLimitContext:
  effective_limit: float | None = None
  unit: str = 'm/s'
  source: str = 'NONE'
  confidence: float = 0.0
  age: float = 0.0
  valid: bool = False
  limit_type: str = 'UNKNOWN'
  road_id: str = ''
  road_match_confidence: float = 0.0
  direction_match: bool = False
  camera_limit: float | None = None
  map_limit: float | None = None
  next_limit: float | None = None
  distance_to_next_limit: float | None = None
  control_eligible: bool = False
  conflict: bool = False
  reason: str = 'NO_SOURCE'
  observations: list = field(default_factory=list)
  rejected: list = field(default_factory=list)
