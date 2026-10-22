"""Short-baseline camera triangulation against the calibrated, driven road.

An image label is insufficient. This adapter requires valid recorded motion,
multiple views of one static object, a current road model and a unique map match.
It never substitutes vehicle heading for a measured sign-facing direction.
"""
from collections import deque
from dataclasses import dataclass, replace
import math
import numpy as np

from openpilot.selfdrive.road_context.camera_geometry import device_frame_from_view_frame, rot_from_euler
from openpilot.selfdrive.road_context.types import finite


@dataclass(frozen=True)
class RoadProof:
  road_id: str
  frame_id: int
  timestamp: float
  frames: tuple
  distance: float
  lateral: float
  right_edge: float
  height: float
  lateral_std: float
  reprojection_pixels: float
  baseline: float
  road_confidence: float
  passage_timestamp: float | None = None
  passage_distance: float | None = None
  passage_std: float | None = None

  def is_passage(self, sign):
    return (finite(self.passage_timestamp,self.passage_distance,self.passage_std,sign.event_timestamp)
            and abs(self.passage_timestamp-sign.event_timestamp)<=1e-6
            and 0 < self.passage_timestamp-self.timestamp <= 4
            and 0 <= self.passage_std <= 5
            and -15 <= self.passage_distance and self.passage_distance+3*self.passage_std < 0)

  def valid_for(self, sign, match):
    return (bool(self.road_id) and finite(self.timestamp,sign.timestamp)
            and all(type(f) is int and f>=0 for f in self.frames)
            and self.road_id == match.road_id == sign.road_id and self.frame_id == sign.frame_id
            and abs(self.timestamp-sign.timestamp) <= 1e-6
            and len(self.frames) >= 3 and len(set(self.frames)) == len(self.frames)
            and self.frames[-1] == sign.frame_id and all(b>a for a,b in zip(self.frames[:-1],self.frames[1:]))
            and finite(self.distance,self.lateral,self.right_edge,self.height,self.lateral_std,self.road_confidence,
                       self.reprojection_pixels,self.baseline)
            and 5 <= self.distance <= 80 and -.3 <= self.lateral-self.right_edge <= 3.5
            and .5 <= self.height <= 4 and 0 <= self.lateral_std <= .6
            and 0 <= self.reprojection_pixels <= 6 and self.baseline >= 3 and .85<=self.road_confidence<=1)


class RoadAssociation:
  def __init__(self, camera_models=None):
    self.camera_models = camera_models or {}
    self.camera = None
    self.camera_key = None
    self.motion = deque(maxlen=256)
    self.models = deque(maxlen=80)
    self.calibrations = deque(maxlen=12)
    self.tracks = {}
    self.pending = {}
    self.last_rejections = []
    self.road_anchor = None

  def configure(self, device, sensor, width, height):
    configuration = self.camera_models.get((device,sensor))
    camera = getattr(configuration, 'fcam', getattr(configuration, 'narrow_road', None))
    key = (device,sensor,width,height)
    if key != self.camera_key:
      self.tracks.clear()
      self.pending.clear()
    self.camera_key = key
    self.camera = camera if camera is not None and camera.size == (width,height) else None

  def add_motion(self, data, valid=True):
    try:
      stamp = data['timestamp']/1e9
      fields = [data[name] for name in ('orientationNED','velocityDevice','angularVelocityDevice')]
      arrays = [np.array([m[k] for k in ('x','y','z','xStd','yStd','zStd')],dtype=float) for m in fields]
      if (not valid or not all(data[k] for k in ('inputsOK','posenetOK','sensorsOK'))
          or not all(m['valid'] for m in fields) or not finite(stamp)
          or not all(np.isfinite(a).all() and (a[3:]>=0).all() for a in arrays)
          or np.max(arrays[1][3:]) > 1. or np.max(arrays[2][3:]) > .03):
        self.motion.clear()
        self.tracks.clear()
        self.pending.clear()
        return
      angles, velocity, angular = arrays
      position = np.zeros(3)
      if self.motion:
        previous = self.motion[-1]
        dt = stamp-previous['time']
        if dt == 0:
          return
        if not 0 < dt <= .15:
          self.motion.clear()
          self.tracks.clear()
          self.pending.clear()
        else:
          angles[:3] = previous['angles'] + (angles[:3]-previous['angles']+np.pi)%(2*np.pi)-np.pi
          world_velocity = rot_from_euler(angles[:3])@velocity[:3]
          position = previous['position']+(previous['velocity']+world_velocity)*dt*.5
      rotation = rot_from_euler(angles[:3])
      self.motion.append(dict(time=stamp,angles=angles[:3],position=position,
                              velocity=rotation@velocity[:3],velocity_std=float(max(velocity[3:])),
                              angular_std=float(max(angular[3:]))))
    except (KeyError,TypeError,ValueError):
      self.motion.clear()
      self.tracks.clear()
      self.pending.clear()

  def add_model(self, data, valid=True):
    if not valid:
      self.models.clear()
      return
    if valid and isinstance(data.get('frameId'),int):
      if self.models and data['frameId'] <= self.models[-1]['frameId']:
        if data['frameId'] == self.models[-1]['frameId']:
          return
        self.models.clear()
        self.tracks.clear()
        self.pending.clear()
      self.models.append(data)

  def add_calibration(self, data, timestamp, valid=True):
    rpy=data.get('rpyCalib',[])
    if (valid and finite(timestamp) and data.get('calStatus')=='calibrated'
        and len(rpy)==3 and finite(*rpy)):
      if self.calibrations and np.linalg.norm(np.array(rpy)-np.array(self.calibrations[-1][1]['rpyCalib']))>.01:
        self.pending.clear()
        self.tracks.clear()
      self.calibrations.append((timestamp,data))
    else:
      self.calibrations.clear()
      self.pending.clear()
      self.tracks.clear()

  def pose(self, stamp):
    for left,right in zip(self.motion,list(self.motion)[1:]):
      if left['time'] <= stamp <= right['time']:
        dt = right['time']-left['time']
        if not 0 < dt <= .15:
          return None
        weight = (stamp-left['time'])/dt
        return dict(position=left['position']*(1-weight)+right['position']*weight,
                    rotation=rot_from_euler(left['angles']*(1-weight)+right['angles']*weight),
                    velocity_std=max(left['velocity_std'],right['velocity_std']),
                    angular_std=max(left['angular_std'],right['angular_std']))
    return None

  def associate(self, sign, now, match):
    from openpilot.selfdrive.road_context.tsr import confirmed_evidence
    if (self.camera is None or not isinstance(sign.track_token, str) or not sign.track_token
        or not isinstance(sign.bbox, (tuple, list)) or len(sign.bbox) != 4
        or not finite(now, sign.timestamp, *sign.bbox) or type(sign.frame_id) is not int
        or not 0 <= now-sign.timestamp <= .5 or sign.kind not in ('LIMIT','END_LIMIT','END_ALL')):
      return sign, 'NO_FRESH_CAMERA_PROOF'
    pose = self.pose(sign.timestamp)
    if pose is None:
      return sign, 'MOTION_UNAVAILABLE'
    x1,y1,x2,y2 = sign.bbox
    w,h = (x2-x1)*self.camera.width,(y2-y1)*self.camera.height
    if (not finite(x1,y1,x2,y2) or not all(0<=v<=1 for v in sign.bbox)
        or min(w,h)<=0 or min(w,h)/max(w,h)<.8):
      return sign, 'OBLIQUE_OR_INVALID_SIGN'
    pixel = np.array([(x1+x2)*self.camera.width/2,(y1+y2)*self.camera.height/2,1.])
    ray = pose['rotation']@device_frame_from_view_frame@self.camera.intrinsics_inv@pixel
    ray /= np.linalg.norm(ray)
    token = (sign.track_token,sign.kind,sign.value_kph)
    track = self.tracks.setdefault(token,[])
    if any(item['frame'] == sign.frame_id for item in track):
      return sign, 'REPEATED_SIGN_FRAME'
    track[:] = [item for item in track if 0 < sign.timestamp-item['stamp'] <= 2.2]
    if any(item['frame'] >= sign.frame_id for item in track):
      return sign, 'REORDERED_SIGN'
    track.append(dict(stamp=sign.timestamp,frame=sign.frame_id,pose=pose,ray=ray))
    self.tracks = {key:items for key,items in self.tracks.items() if items and 0<=now-items[-1]['stamp']<=1.}
    if len(track)<3 or track[-1]['stamp']-track[0]['stamp']<.2:
      return sign, 'MORE_VIEWS_REQUIRED'
    if not confirmed_evidence(sign):
      return sign, 'UNCONFIRMED_VISUAL_EVIDENCE'
    centers = np.array([item['pose']['position'] for item in track])
    rays = np.array([item['ray'] for item in track])
    baseline = float(np.linalg.norm(centers[-1]-centers[0]))
    if baseline<3:
      return sign, 'INSUFFICIENT_PARALLAX'
    masks = np.array([np.eye(3)-np.outer(r,r) for r in rays])
    matrix = masks.sum(axis=0)
    if np.linalg.cond(matrix)>1e6:
      return sign, 'ILL_CONDITIONED_POSITION'
    point = np.linalg.solve(matrix,np.einsum('nij,nj->i',masks,centers))
    directions = point-centers
    distances = np.linalg.norm(directions,axis=1)
    if np.any(np.einsum('ij,ij->i',directions,rays)<=0):
      return sign, 'BEHIND_CAMERA'
    residual = float(np.max(np.linalg.norm(directions/distances[:,None]-rays,axis=1))*self.camera.focal_length)
    if residual>6:
      return sign, 'INCONSISTENT_STATIC_OBJECT'
    calibration = next(((t,c) for t,c in reversed(self.calibrations) if 0<=sign.timestamp-t<=3),None)
    model = next((m for m in reversed(self.models) if 0<=sign.frame_id-m['frameId']<=4),None)
    if calibration is None or model is None:
      return sign, 'CALIBRATION_OR_MODEL_UNAVAILABLE'
    try:
      _,calibration = calibration
      rpy = calibration['rpyCalib']
      if calibration['calStatus']!='calibrated' or len(rpy)!=3 or not finite(*rpy):
        return sign, 'UNCALIBRATED'
      transform = rot_from_euler(rpy).T@pose['rotation'].T
      calibrated = transform@(point-centers[-1])
      edge = model['roadEdges'][1]
      edge_std = model['roadEdgeStds'][1]
      coordinates = np.array([edge[k] for k in ('x','y','z')],dtype=float)
      if (coordinates.ndim!=2 or coordinates.shape[1]<3 or not np.isfinite(coordinates).all()
          or not np.all(np.diff(coordinates[0])>0) or not finite(edge_std) or not 0<=edge_std<=.8
          or not coordinates[0,0]<=calibrated[0]<=coordinates[0,-1]):
        return sign, 'UNCERTAIN_ROAD_EDGE'
      right = float(np.interp(calibrated[0],coordinates[0],coordinates[1]))
      ground = float(np.interp(calibrated[0],coordinates[0],coordinates[2]))
      # Position uncertainty includes pixel localization and measured motion uncertainty.
      covariance = np.linalg.inv(matrix)*float(np.mean(distances**2))*(max(2.,residual)/self.camera.focal_length)**2
      covariance = transform@covariance@transform.T
      span = track[-1]['stamp']-track[0]['stamp']
      motion_error = max(item['pose']['velocity_std'] for item in track)*span
      rotation_error = max(item['pose']['angular_std'] for item in track)*span*float(calibrated[0])
      lateral_std = math.sqrt(max(0.,float(covariance[1,1]))+motion_error**2+rotation_error**2)
      diameter = max(w,h)*float(calibrated[0])/self.camera.focal_length
      if not .25<=diameter<=1.6:
        return sign, 'IMPLAUSIBLE_SIGN_SIZE'
      road_confidence = match.confidence
      # A short drop in lateral GPS agreement must not erase a recently
      # established road identity. This only qualifies camera geometry, never
      # raises the map's own confidence or grants map speed authority.
      anchor=self.road_anchor
      if (anchor is not None and 0<=now-anchor[0]<=2
          and match.road_id==anchor[1].road_id and match.forward==anchor[1].forward
          and match.direction_match and match.reason=='MATCHED'
          and finite(match.confidence,match.distance,match.along)
          and .7<=match.confidence<=1 and 0<=match.distance<=5
          and 0<=match.along-anchor[1].along<=60):
        road_confidence=max(road_confidence,anchor[1].confidence)
      previous = self.pending.get(token)
      if (previous is not None and previous['sign'].road_id==match.road_id
          and 0<sign.timestamp-previous['sign'].timestamp<=1. and match.direction_match
          and finite(match.confidence) and match.confidence>=.7):
        road_confidence = max(road_confidence,previous['sign'].road_proof.road_confidence)
      proof = RoadProof(match.road_id,sign.frame_id,sign.timestamp,tuple(item['frame'] for item in track),
                        float(calibrated[0]),float(calibrated[1]),right,ground-float(calibrated[2]),
                        lateral_std,residual,baseline,road_confidence)
      associated = replace(sign,road_id=match.road_id,distance=proof.distance,road_proof=proof)
      if not match.road_id or not match.direction_match or not finite(road_confidence) or road_confidence<.85:
        return associated, 'UNCERTAIN_MAP_ROAD'
      if not proof.valid_for(associated,match):
        return sign, 'OUTSIDE_VERIFIED_ROADSIDE'
      associated = replace(associated,qualified=True)
      self.pending[token] = dict(sign=associated,point=point,calibration=rot_from_euler(rpy),
                                 range_std=math.sqrt(max(0.,float(covariance[0,0]))+motion_error**2))
      return associated, 'CALIBRATED_OWN_ROADSIDE'
    except (KeyError,IndexError,TypeError,ValueError,np.linalg.LinAlgError):
      return sign, 'INVALID_ROAD_GEOMETRY'

  def qualify(self, signs, now, match):
    if (match.road_id and match.direction_match and finite(match.confidence)
        and .85<=match.confidence<=1):
      self.road_anchor=(now,match)
    elif (self.road_anchor is not None and
          (match.road_id!=self.road_anchor[1].road_id or not match.direction_match
           or match.forward!=self.road_anchor[1].forward or match.confidence<.7)):
      self.road_anchor=None
    result = []
    self.last_rejections = []
    for sign in signs:
      associated,reason = self.associate(sign,now,match)
      result.append(associated)
      if not associated.qualified:
        self.last_rejections.append(dict(frame=sign.frame_id,kind=sign.kind,reason=reason))
    return result

  def passages(self, now, match):
    """Emit a new applicability event, retaining the original visual evidence."""
    self.pending={token:p for token,p in self.pending.items() if 0<=now-p['sign'].timestamp<=4}
    if not self.motion:
      return []
    motion = self.motion[-1]
    if not 0 <= now-motion['time'] <= .15:
      return []
    if not self.models or not self.calibrations:
      return []
    if not 0<=now-self.calibrations[-1][0]<=3:
      return []
    model = self.models[-1]
    try:
      model_time = model['timestampEof']/1e9
      right_edge = model['roadEdges'][1]['y'][0]
      edge_std = model['roadEdgeStds'][1]
      if (not finite(model_time,right_edge,edge_std) or not 0<=now-model_time<=.3
          or not 0<=edge_std<=.8):
        return []
    except (KeyError,TypeError,IndexError):
      return []
    result=[]
    for token,pending in list(self.pending.items()):
      sign=pending['sign']
      elapsed=motion['time']-sign.timestamp
      if elapsed>4 or elapsed<0 or match.road_id!=sign.road_id:
        del self.pending[token]
        continue
      # Passage is a measured geometric event on an already associated object.
      # Current map uncertainty still blocks MAP control in RegulatoryFusion.
      if not match.direction_match or not finite(match.confidence) or not 0<=match.confidence<=1:
        continue
      point=pending['calibration'].T@rot_from_euler(motion['angles']).T@(pending['point']-motion['position'])
      if not -.3<=float(point[1])-right_edge<=3.5:
        continue
      uncertainty=math.hypot(pending['range_std'],motion['velocity_std']*elapsed)
      proof=replace(sign.road_proof,passage_timestamp=motion['time'],passage_distance=float(point[0]),
                    passage_std=uncertainty)
      passed=replace(sign,road_proof=proof,event_timestamp=motion['time'])
      if proof.is_passage(passed):
        result.append(passed)
        del self.pending[token]
    return result
