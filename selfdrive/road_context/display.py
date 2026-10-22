"""Pure display selection. A displayed map limit is not control authorization."""
import math
import json


def speed_sign(context, now):
  if context is None or not math.isfinite(now) or not 0 <= now-context.timestamp <= .6:
    return None
  speed = context.speed
  if (not speed.valid or speed.conflict or speed.unit != 'm/s'
      or not math.isfinite(speed.effectiveLimit) or not 0 < speed.effectiveLimit <= 70
      or not math.isfinite(speed.confidence) or not .7 <= speed.confidence <= 1
      or not math.isfinite(speed.age) or not 0 <= speed.age <= 30):
    return None
  return round(speed.effectiveLimit * 3.6)


def camera_candidate(context, now):
  """Display-only OCR proposal. Never consumed by a controller."""
  if (context is None or not math.isfinite(now) or not 0 <= now-context.timestamp <= .6
      or not isinstance(context.diagnostics, str) or len(context.diagnostics) > 100000):
    return None
  try:
    data = json.loads(context.diagnostics)
    if not isinstance(data, dict) or not isinstance(data.get('signs', []), list):
      return None
    signs = data.get('signs', [])
    candidates = [s for s in signs if isinstance(s, dict) and s.get('source') == 'OCR_CONSENSUS_RED_CIRCLE_UNVALIDATED'
                  and s.get('kind') == 'LIMIT' and type(s.get('value_kph')) is int and s['value_kph'] in range(20, 131, 10)
                  and type(s.get('confidence')) in (int, float) and .90 <= s['confidence'] <= 1
                  and type(s.get('timestamp')) in (int, float) and 0 <= now-s['timestamp'] <= .6]
    values = {s['value_kph'] for s in candidates}
    return values.pop() if len(values) == 1 else None
  except (ValueError, TypeError, KeyError):
    return None


CAMERA_WARNING_KINDS = {
  'FIXED_SPEED_CAMERA', 'MOBILE_CAMERA_ZONE', 'RED_LIGHT_CAMERA', 'RED_LIGHT_SPEED_CAMERA',
  'AVERAGE_SPEED_START', 'AVERAGE_SPEED_ZONE', 'AVERAGE_SPEED_END', 'DANGER_ZONE',
}


def speed_camera_warning(context, now):
  """Return the nearest fresh, policy-approved enforcement warning for display only."""
  if context is None or not math.isfinite(now) or not 0 <= now - context.timestamp <= .6:
    return None
  if not context.warningsEnabled:
    return None

  candidates = []
  for item in context.enforcement:
    kind, distance = item.kind, item.distance
    if kind not in CAMERA_WARNING_KINDS or not math.isfinite(distance) or not 0 <= distance <= 1500:
      continue
    candidates.append(item)
  return min(candidates, key=lambda warning: warning.distance) if candidates else None
