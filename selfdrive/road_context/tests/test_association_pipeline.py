"""End-to-end display association and nonblocking ingestion regressions."""
from dataclasses import replace
from types import SimpleNamespace
from threading import Event
import numpy as np
import pytest
from openpilot.selfdrive.road_context.association_input import feed_association
from openpilot.selfdrive.road_context.camera_geometry import rot_from_euler
from openpilot.selfdrive.road_context.display import speed_sign
from openpilot.selfdrive.road_context.engine import RoadContextEngine
from openpilot.selfdrive.road_context.ocr_vision import SignConsensus
from openpilot.selfdrive.road_context.tsr import confirmed_evidence, speed_observation
from openpilot.selfdrive.road_context.types import LimitObservation, Position
from openpilot.selfdrive.road_context.vision_worker import VisionWorker
from openpilot.selfdrive.road_context.tests.test_road_association import setup, observation, motion, MATCH


def test_current_pose_and_calibration_names_use_actual_event_time():
  adapter = setup()
  adapter.motion.clear()
  current = motion(10.)
  current.pop('timestamp')
  feed_association(adapter, 'livePose', current, 11.)
  assert adapter.motion[-1]['time'] == 11.
  old = motion(11.05)
  feed_association(adapter, 'deviceMotion', old, 11.06)
  assert adapter.motion[-1]['time'] == 11.05
  feed_association(adapter, 'liveCalibration', dict(calStatus='calibrated', rpyCalib=[0.,0.,0.]), 11.)
  assert adapter.calibrations[-1][0] == 11.
  feed_association(adapter, 'livePose', current, 11.1, valid=False)
  assert not adapter.motion


def test_rotation_is_orthonormal_for_recorded_nonzero_angles():
  rotation = rot_from_euler((.0134, .0865, -2.3484))
  np.testing.assert_allclose(rotation.T@rotation, np.eye(3), atol=1e-12)
  assert np.linalg.det(rotation) == pytest.approx(1.)


def test_provisional_views_build_geometry_but_not_authority():
  adapter = setup()
  tracker = SignConsensus()
  results = []
  for t, frame in ((10.2,1004),(10.4,1008),(10.6,1012)):
    raw = replace(observation(t,frame), evidence=(), semantic_timestamp=None, track_token='')
    candidate = tracker.update([raw],t,frame,include_provisional=True)[0]
    results.append(adapter.associate(candidate,t+.05,MATCH)[0])
  assert [confirmed_evidence(s) for s in results] == [False,False,True]
  assert [s.qualified for s in results] == [False,False,True]
  assert results[-1].facing_heading is None
  assert not speed_observation(results[-1],MATCH,0.).control_eligible


def test_slow_two_hz_observations_keep_only_recent_evidence():
  tracker = SignConsensus()
  for i in range(20):
    t = 10.+i*.5
    raw = replace(observation(10.,1000), timestamp=t, frame_id=1000+i)
    signs = tracker.update([raw],t,1000+i,include_provisional=True)
    if i>=2:
      assert confirmed_evidence(signs[0])
      assert signs[0].evidence[-1][1]-signs[0].evidence[0][1] <= 2.2


def test_association_reaches_fusion_and_hud_without_control_eligibility():
  class Maps:
    def match(self,*args): return MATCH
    def observation(self,match,now,epoch): return LimitObservation('MAP',None,now,.99,'a')
    def upcoming(self,*args): return None
    def get(self,*args): return None
  engine = RoadContextEngine(Maps())
  engine.association = setup()
  for t,frame in ((10.2,1004),(10.4,1008),(10.6,1012)):
    result = engine.update(t+.05,1000.,Position(47.,19.,0.,10.,t),[observation(t,frame)],dict(camera_available=True))
  speed = result['speed']
  assert speed['valid'] and speed['source']=='CAMERA'
  assert not speed['control_eligible']
  wire = SimpleNamespace(timestamp=10.65,speed=SimpleNamespace(valid=True,conflict=False,unit='m/s',effectiveLimit=speed['effective_limit'],confidence=speed['confidence'],age=speed['age']))
  assert speed_sign(wire,10.7)==30
  assert speed_sign(wire,12.) is None


def test_repeated_frame_does_not_erase_geometric_history():
  adapter=setup()
  sign=observation(10.2,1004)
  adapter.associate(sign,10.25,MATCH)
  token=next(iter(adapter.tracks))
  assert adapter.associate(sign,10.26,MATCH)[1]=='REPEATED_SIGN_FRAME'
  assert len(adapter.tracks[token])==1


def test_slow_ocr_does_not_block_or_queue_more_camera_frames():
  gate=Event()
  class Vision:
    def process(self,rgb,stamp,frame):
      gate.wait(2.)
      return [],dict(camera_available=True)
  worker=VisionWorker(Vision())
  try:
    assert worker.submit(None,10.,1)
    assert worker.poll(10.1) is None
    assert not worker.submit(None,10.1,2)
    gate.set()
    worker.pending.result(timeout=2.)
    assert worker.poll(11.)[1]['camera_available'] is False
    assert worker.pending is None
  finally:
    gate.set()
    worker.close()
