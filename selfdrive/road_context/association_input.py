"""Receive-only adaptation of old recorded and current NAP message names."""
from openpilot.selfdrive.road_context.types import finite


def feed_association(association, kind, data, timestamp, valid=True):
  if kind in ('livePose', 'deviceMotion'):
    motion = dict(data)
    # Current livePose has no internal timestamp. Use its actual event time.
    if finite(timestamp) and (kind == 'livePose' or 'timestamp' not in motion):
      motion['timestamp'] = round(timestamp*1e9)
    association.add_motion(motion, valid)
  elif kind == 'modelV2':
    association.add_model(data, valid)
  elif kind in ('liveCalibration', 'extrinsicsCalibration'):
    association.add_calibration(data, timestamp, valid)
