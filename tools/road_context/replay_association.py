"""Replay recorded model/motion/calibration plus real encoder timestamps."""
import argparse
from collections import Counter
from dataclasses import asdict, fields, replace
import json
from pathlib import Path
import sys
import types


def main():
  parser=argparse.ArgumentParser(description=__doc__)
  parser.add_argument('--archive',type=Path,required=True)
  parser.add_argument('--output',type=Path,required=True)
  args=parser.parse_args()
  root=Path(__file__).resolve().parents[2]
  package=types.ModuleType('openpilot');package.__path__=[str(root)];sys.modules['openpilot']=package
  from openpilot.selfdrive.road_context.association_input import feed_association
  from openpilot.selfdrive.road_context.camera_geometry import CameraGeometry
  from openpilot.selfdrive.road_context.road_association import RoadAssociation
  from openpilot.selfdrive.road_context.tsr import SignObservation,relevance
  from openpilot.selfdrive.road_context.types import RoadMatch
  scene=json.loads((args.archive/'road-association/207.json').read_text())
  events=sorted(scene['events'],key=lambda e:e['time'])
  encoded={e['data']['segmentId']:e['data'] for e in events if e['kind']=='narrowRoadEncodeIdx'}
  runs=[r for r in json.loads((args.archive/'concurrent-v22.json').read_text()) if str(r['clip'])=='207']
  names={f.name for f in fields(SignObservation)}
  outputs=[]
  # Intrinsics match the recorded OX03C10 camera and the NAP DEVICE_CAMERAS registry.
  config=types.SimpleNamespace(fcam=CameraGeometry(1928,1208,2648.))
  for scenario in ('recorded','opposite_side','missing_calibration','missing_model'):
    for run in runs:
      adapter=RoadAssociation({('tici','ox03c10'):config})
      adapter.configure('tici','ox03c10',1928,1208)
      cursor=0;match=RoadMatch();heading=0.;observations=[];reasons=Counter()
      for saved in run['signs']:
        frame=encoded[saved['frame_id']]
        evidence=[];semantic=None
        for local_id,old_stamp in saved.get('evidence',[]):
          actual=encoded[local_id];stamp=actual['timestampSof']/1e9
          evidence.append((actual['frameId'],stamp))
          if saved.get('semantic_timestamp') is not None and abs(old_stamp-saved['semantic_timestamp'])<1e-6:
            semantic=stamp
        data={key:value for key,value in saved.items() if key in names}
        data.update(frame_id=frame['frameId'],timestamp=frame['timestampSof']/1e9,evidence=tuple(evidence),semantic_timestamp=semantic)
        sign=SignObservation(**data);now=sign.timestamp+.4
        if scenario=='opposite_side':
          x1,y1,x2,y2=sign.bbox;sign=replace(sign,bbox=(1-x2,y1,1-x1,y2))
        while cursor<len(events) and events[cursor]['time']<=now:
          event=events[cursor];cursor+=1
          kind=event['kind'];value=event['data'];valid=event['valid']
          if kind in ('deviceMotion','livePose','modelV2','extrinsicsCalibration','liveCalibration'):
            if scenario=='missing_model' and kind=='modelV2':valid=False
            if scenario=='missing_calibration' and kind in ('extrinsicsCalibration','liveCalibration'):valid=False
            feed_association(adapter,kind,value,event['time'],valid)
          elif kind=='gpsLocation':heading=value.get('bearingDeg',0.)
          elif kind=='roadContext':
            speed=value['speed']
            match=RoadMatch(road_id=speed.get('roadId',''),confidence=speed.get('roadMatchConfidence',0.),direction_match=speed.get('directionMatch',False))
            adapter.qualify([],event['time'],match)
        associated,reason=adapter.associate(sign,now,match)
        score,proof_reason=relevance(associated,match,heading)
        reasons[reason]+=1
        observations.append(dict(frame=sign.frame_id,timestamp=sign.timestamp,kind=sign.kind,qualified=associated.qualified,
                                 reason=reason,relevance=score,relevance_reason=proof_reason,map_confidence=match.confidence,
                                 proof=asdict(associated.road_proof) if associated.road_proof else None))
      outputs.append(dict(scenario=scenario,clip='207',phase=run['phase'],observations=observations,
                          qualified=sum(o['qualified'] for o in observations),reasons=dict(reasons)))
  totals={scenario:sum(r['qualified'] for r in outputs if r['scenario']==scenario) for scenario in ('recorded','opposite_side','missing_calibration','missing_model')}
  report=dict(event_counts=dict(Counter(e['kind'] for e in events)),qualified=totals,runs=outputs,
              limitations=['Only clip 207 has locally retained synchronized model/motion/calibration records.',
                           'Sign events are historical detector outputs; no new pixel-level OCR is claimed.',
                           'Road identity/confidence comes from the recorded roadContext, not a new GPS/map match.',
                           'Recorded section contains END_ALL; numeric LIMIT geometry has separate synthetic integration tests.',
                           'No device or driving validation is established.'])
  args.output.parent.mkdir(parents=True,exist_ok=True)
  args.output.write_text(json.dumps(report,indent=2),encoding='utf-8')
  print(json.dumps({k:v for k,v in report.items() if k!='runs'},indent=2))
  if any(totals[s] for s in ('opposite_side','missing_calibration','missing_model')):
    raise SystemExit('Negative replay scenario incorrectly qualified a sign')


if __name__=='__main__':main()
