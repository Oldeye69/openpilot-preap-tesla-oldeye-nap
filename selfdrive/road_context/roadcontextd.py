"""Optional receive-only context process. No car interface, Panda, or CAN publisher."""

import json
import os
import sqlite3
import time
from pathlib import Path

from cereal import messaging
from msgq.visionipc import VisionIpcClient, VisionStreamType
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.road_context.engine import RoadContextEngine
from openpilot.selfdrive.road_context.enforcement import EnforcementPoint, EnforcementPolicy
from openpilot.selfdrive.road_context.maps import OSMProvider, RegionalOSMProvider
from openpilot.selfdrive.road_context.types import Position
from openpilot.selfdrive.road_context.vision import PrototypeVision, nv12_rgb
from openpilot.selfdrive.road_context.ocr_vision import OCRVision
from openpilot.selfdrive.road_context.wire import publish_message


def load_points(path):
  document = json.loads(Path(path).read_text())
  if document.get('schema_version') != 1 or not document.get('license') or not document.get('version'):
    raise ValueError('Enforcement catalog needs version and license metadata')
  return [EnforcementPoint(**point) for point in document['points']]


def position_from_messages(sm, now):
  for name in ('gpsLocationExternal', 'gpsLocation'):
    gps = sm[name]
    if sm.valid[name] and gps.hasFix:
      position = Position(gps.latitude, gps.longitude, gps.bearingDeg, gps.speed, sm.logMonoTime[name] / 1e9, gps.horizontalAccuracy)
      if position.fresh(now):
        return position
  return None


def apply_policy_action(policy, text, now):
  try:
    action = json.loads(text)
    if action['country'] == policy.country and action['prompt'] == policy.prompt and 0 <= now - float(action['timestamp']) <= 5:
      return policy.choose(action['action'])
  except (ValueError, TypeError, KeyError):
    pass
  return False


def param_text(params, key, default=""):
  value = params.get(key, return_default=True)
  if isinstance(value, bytes):
    return value.decode("utf-8", "replace")
  return value or default


def main():
  os.nice(10)
  params = Params()
  try:
    map_path = param_text(params, 'RoadContextMapPath')
    if not map_path:
      raise ValueError('map path is not configured')
    maps = RegionalOSMProvider(map_path) if Path(map_path).is_dir() else OSMProvider(map_path)
  except (OSError, ValueError, sqlite3.Error, KeyError):
    maps = None
    cloudlog.warning('RoadContext map unavailable; camera context only')
  try:
    points = load_points(param_text(params, 'RoadContextEnforcementPath'))
  except (OSError, ValueError, TypeError, KeyError):
    points = []
    cloudlog.warning('RoadContext enforcement catalog unavailable')
  try:
    policy = EnforcementPolicy(
      param_text(params, 'RoadContextPolicyPath') or None,
      enabled=params.get_bool('SpeedCameraWarnings'),
      disabled_reason=param_text(params, 'RoadContextWarningDisabledReason'),
    )
  except (OSError, ValueError, KeyError):
    policy = EnforcementPolicy()  # OFF on invalid external policy
  engine, vision = RoadContextEngine(maps, points, policy), PrototypeVision()
  try:
    vision = OCRVision('/data/road-context/ocr-best/eng.traineddata')
  except (ImportError, OSError, ValueError, RuntimeError):
    cloudlog.exception('RoadContext OCR unavailable; unqualified prototype fallback')
  sm = messaging.SubMaster(['gpsLocationExternal', 'gpsLocation'])
  pm = messaging.PubMaster(['roadContext'])
  camera = VisionIpcClient('camerad', VisionStreamType.VISION_STREAM_ROAD, True)
  connected, last_process, last_frame, stamp = False, -1.0, -1, -1.0
  signs, lights = [], {'camera_available': False}
  while (params.get_bool('RoadContextEnabled') or params.get_bool('SpeedCameraWarnings')
         or params.get_bool('AutoHighBeamEnabled')):
    start = time.monotonic()
    sm.update(0)
    enabled = params.get_bool('SpeedCameraWarnings')
    if enabled != policy.enabled:
      policy.manual(enabled)
      params.put('RoadContextWarningDisabledReason', policy.disabled_reason)
    text = params.get('RoadContextPolicyAction')
    if text:
      if apply_policy_action(policy, text, start):
        params.put_bool('SpeedCameraWarnings', policy.enabled)
        params.put('RoadContextWarningDisabledReason', policy.disabled_reason)
      params.remove('RoadContextPolicyAction')
    if start - last_process >= 0.5:
      last_process = start
      if not connected:
        connected = camera.connect(False)
      frame = camera.recv(0) if connected else None
      if frame is not None and frame.frame_id != last_frame:
        stamp = camera.timestamp_sof / 1e9
        last_frame = frame.frame_id
        if 0 <= start - stamp <= 0.5:
          try:
            signs, lights = vision.process(nv12_rgb(frame.data, frame.width, frame.height, frame.stride, frame.uv_offset, step=2), stamp, last_frame)
          except Exception:
            cloudlog.exception('RoadContext frame processing failed; map remains independent')
            signs, lights = [], {'camera_available': False}
    if connected and start - stamp > 2.0:
      connected = False  # reconnect after a camera restart instead of retaining an old shared-memory stream
    if not 0 <= start - stamp <= 0.5:
      signs, lights = [], {'camera_available': False}
    try:
      epoch = time.time()  # noqa: TID251 - wall time is required only for source snapshot age
      context = engine.update(
        start,
        epoch,
        position_from_messages(sm, start),
        signs,
        lights,
        camera_timestamp=stamp,
        auto_high_beam=params.get_bool('AutoHighBeamEnabled'),
        driver_high_requested=None,
        driver_timestamp=None,
      )
      pm.send('roadContext', publish_message(context))
    except (ValueError, KeyError, TypeError, sqlite3.Error):
      cloudlog.exception('RoadContext invalid source; suppressing output')
    time.sleep(max(0.0, 0.2 - (time.monotonic() - start)))


if __name__ == '__main__':
  main()
