"""Optional receive-only context process. No car interface, Panda, or CAN publisher."""

import json
import os
import sqlite3
import time
from pathlib import Path

from cereal import messaging
from cereal.services import SERVICE_LIST
from msgq.visionipc import VisionIpcClient, VisionStreamType
from openpilot.common.params import Params
from openpilot.common.transformations.camera import DEVICE_CAMERAS
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.road_context.engine import RoadContextEngine
from openpilot.selfdrive.road_context.enforcement import EnforcementPoint, EnforcementPolicy
from openpilot.selfdrive.road_context.maps import OSMProvider, RegionalOSMProvider
from openpilot.selfdrive.road_context.types import Position
from openpilot.selfdrive.road_context.vision import PrototypeVision, nv12_rgb
from openpilot.selfdrive.road_context.ocr_vision import OCRVision
from openpilot.selfdrive.road_context.wire import publish_message
from openpilot.selfdrive.road_context.road_association import RoadAssociation
from openpilot.selfdrive.road_context.association_input import feed_association
from openpilot.selfdrive.road_context.vision_worker import VisionWorker


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
  engine.association = RoadAssociation(DEVICE_CAMERAS)
  motion_name = next((s for s in ('livePose', 'deviceMotion') if s in SERVICE_LIST), None)
  calibration_name = next((s for s in ('liveCalibration', 'extrinsicsCalibration') if s in SERVICE_LIST), None)
  camera_name = next((s for s in ('roadCameraState', 'narrowRoadCameraState') if s in SERVICE_LIST), None)
  inputs = [s for s in ('gpsLocationExternal', 'gpsLocation', 'modelV2', 'deviceState',
                       motion_name, calibration_name, camera_name) if s is not None and s in SERVICE_LIST]
  sm = messaging.SubMaster(inputs)
  worker = VisionWorker(vision)
  pm = messaging.PubMaster(['roadContext'])
  stream = getattr(VisionStreamType, 'VISION_STREAM_NARROW_ROAD', None)
  if stream is None:
    stream = VisionStreamType.VISION_STREAM_ROAD
  camera = VisionIpcClient('camerad', stream, True)
  connected, last_process, last_frame, stamp = False, -1.0, -1, -1.0
  signs, lights = [], {'camera_available': False}
  processed_stamp, last_publish = -1., -1.
  while (params.get_bool('RoadContextEnabled') or params.get_bool('SpeedCameraWarnings')
        ):
    start = time.monotonic()
    sm.update(0)
    for name in (motion_name, calibration_name, 'modelV2'):
      if name is not None and sm.updated[name]:
        feed_association(engine.association, name, sm[name].to_dict(), sm.logMonoTime[name]/1e9, sm.valid[name])
    signs = []
    try:
      ready = worker.poll(start)
      if ready is not None:
        signs, lights, processed_stamp = ready
    except Exception:
      cloudlog.exception('RoadContext asynchronous perception failed')
      lights = {'camera_available': False}
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
            if camera_name is not None and sm.valid[camera_name] and sm.valid['deviceState']:
              engine.association.configure(str(sm['deviceState'].deviceType), str(sm[camera_name].sensor), frame.width, frame.height)
            else:
              engine.association.configure('', '', frame.width, frame.height)
            rgb = nv12_rgb(frame.data, frame.width, frame.height, frame.stride, frame.uv_offset, step=2)
            worker.submit(rgb, stamp, last_frame)
          except Exception:
            cloudlog.exception('RoadContext frame processing failed; map remains independent')
            signs, lights = [], {'camera_available': False}
    if connected and start - stamp > 2.0:
      connected = False  # reconnect after a camera restart instead of retaining an old shared-memory stream
    if not 0 <= start - processed_stamp <= 0.5:
      signs, lights = [], {'camera_available': False}
    if start - last_publish < .2 and not signs:
      time.sleep(max(0., .02 - (time.monotonic() - start)))
      continue
    last_publish = start
    try:
      epoch = time.time()  # noqa: TID251 - wall time is required only for source snapshot age
      context = engine.update(
        start,
        epoch,
        position_from_messages(sm, start),
        signs,
        lights,
        camera_timestamp=processed_stamp,
        auto_high_beam=False,
        driver_high_requested=None,
        driver_timestamp=None,
      )
      pm.send('roadContext', publish_message(context))
    except (ValueError, KeyError, TypeError, sqlite3.Error):
      cloudlog.exception('RoadContext invalid source; suppressing output')
    time.sleep(max(0.0, .02 - (time.monotonic() - start)))
  worker.close()


if __name__ == '__main__':
  main()
