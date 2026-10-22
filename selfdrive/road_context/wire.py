"""Typed wire boundary. Diagnostics are never parsed by the speed controller."""
import json
import math
from openpilot.selfdrive.road_context.types import SpeedLimitContext


def json_safe(value):
  if isinstance(value, dict):
    return {k: json_safe(v) for k, v in value.items()}
  if isinstance(value, (list, tuple)):
    return [json_safe(v) for v in value]
  if isinstance(value, float) and not math.isfinite(value):
    return None
  return value


def dumps(value):
  return json.dumps(json_safe(value), allow_nan=False, separators=(',', ':'))


def publish_message(context):
  from cereal import messaging
  msg = messaging.new_message('roadContext', valid=True)
  r = msg.roadContext
  r.timestamp, r.schemaVersion = context['timestamp'], 1
  s = context['speed']
  names = {'effective_limit': 'effectiveLimit', 'source': 'source', 'confidence': 'confidence', 'age': 'age',
           'valid': 'valid', 'limit_type': 'limitType', 'road_id': 'roadId', 'road_match_confidence': 'roadMatchConfidence',
           'direction_match': 'directionMatch', 'conflict': 'conflict', 'control_eligible': 'controlEligible', 'reason': 'reason', 'unit': 'unit'}
  for key, name in names.items():
    setattr(r.speed, name, s[key] if s[key] is not None else 0.)
  for key, name, presence in [('camera_limit', 'cameraLimit', 'hasCamera'), ('map_limit', 'mapLimit', 'hasMap'),
                               ('next_limit', 'nextLimit', 'hasNext')]:
    setattr(r.speed, name, s[key] or 0.)
    setattr(r.speed, presence, s[key] is not None)
  r.speed.distanceToNextLimit = s['distance_to_next_limit'] or 0.
  for dest, warning in zip(r.init('enforcement', len(context['enforcement'])), context['enforcement'], strict=True):
    dest.kind, dest.distance = warning['type'], warning['distance']
    dest.limit, dest.hasLimit = warning.get('limit') or 0., warning.get('limit') is not None
    dest.remainingDistance, dest.hasRemaining = warning.get('remaining') or 0., warning.get('remaining') is not None
    dest.source, dest.identity = warning['source'], warning['id']
    dest.eta, dest.hasEta = warning.get('eta') or 0., warning.get('eta') is not None
  light = context['lighting']
  r.lighting.highBeamAllowed, r.lighting.confidence = light['high_beam_allowed'], light['confidence']
  r.lighting.reason, r.lighting.age, r.lighting.timestamp = light['reason'], light['age'], light['timestamp']
  r.lighting.state, r.lighting.requestedLightState = context['ahb']['state'], context['ahb']['requested_light_state']
  r.lighting.ambient = light['ambient']
  policy = context['country_policy']
  r.country, r.policyState, r.policyPrompt = policy['country'], policy['state'], policy['prompt'] or ''
  r.warningsEnabled, r.disabledReason = policy['enabled'], policy['disabled_reason']
  r.diagnostics = dumps(context)
  return msg


def speed_from_wire(r):
  s = r.speed
  return SpeedLimitContext(effective_limit=s.effectiveLimit if s.valid else None, unit=s.unit, source=s.source,
                           confidence=s.confidence, age=s.age, valid=s.valid, limit_type=s.limitType, road_id=s.roadId,
                           road_match_confidence=s.roadMatchConfidence, direction_match=s.directionMatch,
                           camera_limit=s.cameraLimit if s.hasCamera else None, map_limit=s.mapLimit if s.hasMap else None,
                           next_limit=s.nextLimit if s.hasNext else None,
                           distance_to_next_limit=s.distanceToNextLimit if s.hasNext else None,
                           control_eligible=s.controlEligible, conflict=s.conflict, reason=s.reason)
