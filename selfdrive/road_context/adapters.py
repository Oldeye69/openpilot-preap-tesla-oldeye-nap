"""Explicit units and availability for existing/future optional navigation providers."""
from openpilot.selfdrive.road_context.types import LimitObservation


class NavigationAdapter:
  def __init__(self, name='NAVIGATION', maximum_age=10.0):
    if name not in ('NAVIGATION', 'DYNAMIC_MAP'):
      raise ValueError('Invalid speed source')
    self.name, self.maximum_age = name, maximum_age

  def observation(self, payload, now, match):
    if payload is None:
      return None  # selected checkout has no active navigation producer
    units = {'m/s': 1., 'km/h': 1/3.6, 'mph': .44704}
    if payload.get('unit') not in units or 'source_timestamp' not in payload or not payload.get('version'):
      return None
    try:
      value = float(payload['limit']) * units[payload['unit']]
      return LimitObservation(self.name, value, float(payload['source_timestamp']), float(payload['confidence']),
                              str(payload['road_id']), ttl=self.maximum_age, direction_match=payload.get('direction_match') is True,
                              match_confidence=match.confidence, version=payload['version'],
                              dataset_age=float(payload.get('dataset_age', 0)),
                              max_dataset_age=self.maximum_age if self.name == 'DYNAMIC_MAP' else 90*86400., control_eligible=False)
    except (KeyError, ValueError, TypeError):
      return None
