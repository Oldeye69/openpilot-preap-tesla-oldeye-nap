from dataclasses import replace
import numpy as np
import pytest

from openpilot.selfdrive.road_context.road_association import RoadAssociation
from openpilot.selfdrive.road_context.tsr import SignObservation, event_fresh, event_ready, relevance
from types import SimpleNamespace
from openpilot.selfdrive.road_context.camera_geometry import CameraGeometry

def LimitState(value, source, confidence, valid):
  return SimpleNamespace(value=value, source=source, confidence=confidence, valid=valid)
from openpilot.selfdrive.road_context.types import RoadMatch

MATCH = RoadMatch(road_id='a', confidence=.99, direction_match=True)


def xyz(values, std=.01):
  return dict(zip(('x','y','z'), values)) | dict(xStd=std,yStd=std,zStd=std,valid=True)


def motion(t, valid=True):
  return dict(timestamp=round(t*1e9), orientationNED=xyz((0.,0.,0.)),
              velocityDevice=xyz((10.,0.,0.)), angularVelocityDevice=xyz((0.,0.,0.),.001),
              inputsOK=valid,posenetOK=True,sensorsOK=True)


def setup():
  adapter=RoadAssociation({("tici", "ox03c10"): SimpleNamespace(fcam=CameraGeometry(1928, 1208, 2648.))})
  adapter.configure('tici','ox03c10',1928,1208)
  adapter.add_calibration(dict(calStatus='calibrated',rpyCalib=[0.,0.,0.]),10.)
  for i in range(60):
    adapter.add_motion(motion(10.+i*.05))
  edge=dict(x=[0.,10.,100.],y=[2.,2.,2.],z=[1.2,1.2,1.2])
  for frame in range(1000,1021):
    adapter.add_model(dict(frameId=frame,timestampEof=round((10.+(frame-1000)/20)*1e9),
                           roadEdges=[edge,edge],roadEdgeStds=[.2,.2]))
  return adapter


def observation(t, frame, *, point=(20.,3.2,-1.), kind='LIMIT'):
  x,y,z=np.array(point)-np.array([10.*(t-10.),0.,0.])
  u,v=964+2648*y/x,604+2648*z/x
  size=.6*2648/x
  box=((u-size/2)/1928,(v-size/2)/1208,(u+size/2)/1928,(v+size/2)/1208)
  return SignObservation(kind,30 if kind=='LIMIT' else None,.99,t,frame,box,
                         evidence=((frame-4,t-.2),(frame-2,t-.1),(frame,t)),
                         semantic_timestamp=t,track_token='one-sign')


def observe_three(adapter, **kwargs):
  result=None
  for t,frame in ((10.2,1004),(10.4,1008),(10.6,1012)):
    result=adapter.associate(observation(t,frame,**kwargs),t+.05,MATCH)
  return result


def test_static_own_road_sign_is_triangulated_without_invented_heading():
  sign,reason=observe_three(setup())
  assert reason=='CALIBRATED_OWN_ROADSIDE' and sign.qualified
  assert sign.facing_heading is None
  assert sign.distance==pytest.approx(14.,abs=.02)
  assert sign.road_proof.lateral==pytest.approx(3.2,abs=.02)
  assert relevance(sign,MATCH,0.)[0] == .99


@pytest.mark.parametrize('point',[(20.,-3.2,-1.),(30.,7.,-1.),(20.,3.2,2.)])
def test_opposite_side_parallel_road_and_ground_objects_are_not_qualified(point):
  sign,_=observe_three(setup(),point=point)
  assert not sign.qualified


def test_uncertain_map_never_becomes_certain_by_association():
  adapter=setup()
  observe_three(adapter)
  sign,reason=adapter.associate(observation(10.7,1014),10.75,replace(MATCH,confidence=.6))
  assert not sign.qualified and reason=='UNCERTAIN_MAP_ROAD'


def test_motion_loss_camera_mismatch_and_model_loss_fail_closed():
  adapter=setup()
  adapter.add_motion(motion(13.,False))
  assert not observe_three(adapter)[0].qualified
  adapter=setup()
  adapter.configure('tici','ox03c10',100,100)
  assert not observe_three(adapter)[0].qualified
  adapter=setup()
  adapter.models.clear()
  assert not observe_three(adapter)[0].qualified


def test_release_and_increase_wait_for_actual_passage_without_rewriting_image_time():
  adapter=setup()
  sign,_=observe_three(adapter,kind='END_ALL')
  original=sign.timestamp
  assert sign.qualified and not event_ready(sign,LimitState(30/3.6,'CAMERA',1,True))
  # Latest measured pose is beyond the sign; the new event keeps original evidence.
  adapter.add_model(adapter.models[-1] | dict(frameId=1059,timestampEof=12950000000))
  passed=adapter.passages(12.95,MATCH)
  assert len(passed)==1
  assert passed[0].timestamp==original and passed[0].evidence==sign.evidence
  assert passed[0].event_timestamp>original
  assert event_fresh(passed[0],12.95) and event_ready(passed[0],LimitState(30/3.6,'CAMERA',1,True))
  assert not adapter.passages(12.96,MATCH)
  assert not event_fresh(passed[0],14.)


def test_forged_fresh_timestamp_cannot_reanimate_old_visual_observation():
  sign,_=observe_three(setup())
  assert not event_fresh(replace(sign,event_timestamp=20.),20.)


def test_reduction_can_be_anticipated_but_increase_cannot():
  sign,_=observe_three(setup())
  assert event_ready(sign,LimitState(50/3.6,'MAP',1,True))
  assert not event_ready(sign,LimitState(20/3.6,'MAP',1,True))


@pytest.mark.parametrize('failure',['calibration','restart','expired'])
def test_pending_passage_cannot_survive_loss_of_reference(failure):
  adapter=setup()
  observe_three(adapter,kind='END_ALL')
  assert adapter.pending
  if failure=='calibration':
    adapter.add_calibration(dict(calStatus='uncalibrated',rpyCalib=[0.,0.,0.]),12.9)
  elif failure=='restart':
    adapter.add_model(dict(frameId=1))
  else:
    adapter.models.clear()
    adapter.passages(20.,MATCH)
  assert not adapter.pending
  assert not adapter.passages(12.95,MATCH)


@pytest.mark.parametrize('case',['same','other','reverse','ambiguous','expired','backward'])
def test_recent_road_anchor_only_bridges_same_road_continuity(case):
  adapter=setup()
  adapter.qualify([],10.,replace(MATCH,along=10.,reason='MATCHED'))
  current=replace(MATCH,confidence=.75,along=15.,distance=3.,reason='MATCHED')
  if case=='other':current=replace(current,road_id='b')
  if case=='reverse':current=replace(current,forward=False)
  if case=='ambiguous':current=replace(current,confidence=.5,reason='AMBIGUOUS')
  if case=='expired':adapter.road_anchor=(7.,adapter.road_anchor[1])
  if case=='backward':current=replace(current,along=9.)
  result=None
  for t,frame in ((10.2,1004),(10.4,1008),(10.6,1012)):
    result=adapter.qualify([observation(t,frame)],t+.05,current)[0]
  assert result.qualified == (case=='same')
  assert current.confidence == (.5 if case=='ambiguous' else .75)
