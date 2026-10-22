"""Independent sign observations/relevance. Prototype detections are never control-qualified."""
from dataclasses import dataclass
from openpilot.selfdrive.road_context.maps import angle
from openpilot.selfdrive.road_context.types import LimitObservation, finite

NUMERIC_LIMITS = (20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130)
CONTEXT_SIGNS = {'END_LIMIT', 'END_ALL', 'URBAN_ENTRY', 'URBAN_EXIT', 'MOTORWAY_ENTRY', 'MOTORWAY_EXIT', 'EXPRESSWAY_ENTRY', 'EXPRESSWAY_EXIT'}


@dataclass(frozen=True)
class SignObservation:
  kind: str
  value_kph: int | None
  confidence: float
  timestamp: float
  frame_id: int
  bbox: tuple
  road_id: str = ''
  facing_heading: float | None = None
  distance: float | None = None
  temporary: bool = False
  variable: bool = False
  conditional: bool = False
  source: str = 'CAMERA_PROTOTYPE'
  qualified: bool = False
  evidence: tuple = ()
  semantic_timestamp: float | None = None
  track_token: str = ''
  road_proof: object | None = None
  event_timestamp: float | None = None


def confirmed_evidence(sign):
  required = 3 if sign.kind == 'LIMIT' else 2
  if not isinstance(sign.evidence, (tuple, list)) or not required <= len(sign.evidence) <= 8:
    return False
  try:
    frames, stamps = zip(*sign.evidence, strict=True)
    return (all(type(frame) is int and frame >= 0 for frame in frames) and finite(*stamps)
            and frames[-1] == sign.frame_id and abs(stamps[-1]-sign.timestamp) <= 1e-6
            and all(b > a for a, b in zip(frames[:-1], frames[1:]))
            and all(b > a for a, b in zip(stamps[:-1], stamps[1:]))
            and 0 < stamps[-1]-stamps[0] <= 2.2+1e-6
            and (sign.kind != 'LIMIT' or finite(sign.semantic_timestamp)
                 and any(abs(t-sign.semantic_timestamp) <= 1e-6 for t in stamps)))
  except (TypeError, ValueError):
    return False


def event_fresh(sign, now):
  if sign.event_timestamp is None:
    return 0 <= now-sign.timestamp <= .5
  from openpilot.selfdrive.road_context.road_association import RoadProof
  return (isinstance(sign.road_proof, RoadProof) and sign.road_proof.is_passage(sign)
          and 0 <= now-sign.event_timestamp <= .5)


def event_ready(sign, current):
  if sign.road_proof is None:
    return True
  from openpilot.selfdrive.road_context.road_association import RoadProof
  proof = sign.road_proof
  return (isinstance(proof, RoadProof) and (proof.is_passage(sign)
          or sign.kind == 'LIMIT' and current.valid and current.value is not None
          and sign.value_kph is not None and sign.value_kph/3.6 <= current.value))


def relevance(sign, match, heading, *, path_image=None, route=()):
  if sign.kind not in CONTEXT_SIGNS | {'LIMIT'} or (sign.kind == 'LIMIT' and sign.value_kph not in NUMERIC_LIMITS):
    return 0., 'UNSUPPORTED_CLASS'
  if (not isinstance(sign.bbox, (tuple, list)) or len(sign.bbox) != 4
      or not finite(sign.confidence, sign.timestamp, *sign.bbox) or not 0 <= sign.confidence <= 1
      or not all(0 <= v <= 1 for v in sign.bbox)):
    return 0., 'INVALID_OBSERVATION'
  x1, y1, x2, y2 = sign.bbox
  if x2 <= x1 or y2 <= y1:
    return 0., 'INVALID_BOX'
  if sign.road_id and sign.road_id != match.road_id:
    return 0., 'OTHER_ROAD'
  if sign.facing_heading is not None and (not finite(sign.facing_heading, heading) or angle(sign.facing_heading, heading) > 45):
    return 0., 'OPPOSITE_DIRECTION'
  if route and match.road_id not in route:
    return .3, 'ROUTE_MISMATCH'
  if sign.road_proof is not None:
    from openpilot.selfdrive.road_context.road_association import RoadProof
    if not isinstance(sign.road_proof, RoadProof) or not sign.road_proof.valid_for(sign, match):
      return 0., 'INVALID_ROAD_PROOF'
    if not match.direction_match or match.confidence < .7:
      return .3, 'UNCERTAIN_ROAD'
    return min(sign.confidence, sign.road_proof.road_confidence), 'CALIBRATED_OWN_ROADSIDE'
  if not match.direction_match or match.confidence < .7:
    return .3, 'UNCERTAIN_ROAD'
  if path_image is None or not sign.road_id or sign.facing_heading is None:
    return .5, 'UNRESOLVED_LANE_RELEVANCE'
  # Calibrated projection of applicable corridor at sign depth, supplied by adapter.
  left, right = path_image
  if not left <= (x1+x2)/2 <= right:
    return .2, 'OUTSIDE_PATH_CORRIDOR'
  if sign.distance is not None and (not finite(sign.distance) or not 0 < sign.distance <= 300):
    return .2, 'DISTANCE_UNCERTAIN'
  return min(sign.confidence, match.confidence), 'APPLICABLE_CANDIDATE'


def speed_observation(sign, match, heading, **kwargs):
  score, reason = relevance(sign, match, heading, **kwargs)
  kind = 'CONDITIONAL' if sign.conditional else 'VARIABLE' if sign.variable else 'TEMPORARY' if sign.temporary else 'POSTED'
  value = sign.value_kph/3.6 if sign.kind == 'LIMIT' and not sign.conditional and sign.value_kph in NUMERIC_LIMITS else None
  return LimitObservation('CAMERA', value, sign.timestamp, sign.confidence, match.road_id, ttl=20,
                          relevance=score, direction_match=match.direction_match, match_confidence=match.confidence,
                          limit_type=kind, version=sign.source,
                          control_eligible=sign.qualified and sign.road_proof is None, reason=reason)
