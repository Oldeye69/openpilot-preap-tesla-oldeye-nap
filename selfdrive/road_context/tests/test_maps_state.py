import hashlib
import json
from pathlib import Path
import pytest
from openpilot.selfdrive.road_context.map_state import country_state, record_matches, COLORS
from openpilot.selfdrive.road_context import map_downloaderd as worker
from openpilot.selfdrive.road_context.map_sources import MapSource, parse_geofabrik_index
from openpilot.selfdrive.road_context.maps import write_database, Road

class Params:
  def __init__(self, **values): self.values=values
  def get(self,key): return self.values.get(key)
  def put(self,key,value): self.values[key]=value
  def remove(self,key): self.values.pop(key,None)


def proof(path):
  return dict(bytes=path.stat().st_size,mtime_ns=path.stat().st_mtime_ns,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),roads=1,integrity="ok")


def test_installed_turns_green_only_when_verified_file_still_matches(tmp_path):
  path=tmp_path/'HU.sqlite';path.write_bytes(b'map');installed={'HU':proof(path)}
  assert country_state('HU',{'HU'},installed,{},tmp_path)=='installed'
  assert COLORS['installed']==(85,215,120,255)
  path.write_bytes(b'changed')
  assert country_state('HU',{'HU'},installed,{},tmp_path)=='pending'
  assert country_state('HU',set(),installed,{},tmp_path)=='available'


@pytest.mark.parametrize('phase',['queued','downloading','building','verifying','installing','waiting_wifi'])
def test_installation_is_yellow_even_if_old_working_map_exists(tmp_path,phase):
  path=tmp_path/'HU.sqlite';path.write_bytes(b'map')
  assert country_state('HU',{'HU'},{'HU':proof(path)},{'id':'HU','state':phase},tmp_path)=='pending'
  assert COLORS['pending']==(255,210,65,255)


def test_other_country_progress_does_not_recolour_installed_country(tmp_path):
  path=tmp_path/'HU.sqlite';path.write_bytes(b'map')
  assert country_state('HU',{'HU'},{'HU':proof(path)},{'id':'IT','state':'building'},tmp_path)=='installed'
  assert country_state('IT',{'HU','IT'},{'HU':proof(path)},{'id':'IT','state':'building'},tmp_path)=='pending'


def test_error_and_malformed_proof_are_never_green(tmp_path):
  path=tmp_path/'HU.sqlite';path.write_bytes(b'map')
  assert country_state('HU',{'HU'},{'HU':proof(path)},{'id':'HU','state':'error'},tmp_path)=='error'
  assert not record_matches(path,proof(path)|{'integrity':'bad'})
  assert not record_matches(path,proof(path)|{'sha256':''})
  assert country_state('../../HU',{'../../HU'},{},{},tmp_path)=='available'


def test_initial_selection_preserves_preloaded_maps(tmp_path,monkeypatch):
  for code in ['HU','SI','IT']:(tmp_path/(code+'.sqlite')).write_bytes(b'preloaded')
  monkeypatch.setattr(worker,'MAP_DIR',tmp_path)
  sources=[MapSource(c,c,'https://download.geofabrik.de/europe/'+c.lower()+'.osm.pbf',(c,)) for c in ['HU','SI','IT','DE']]
  p=Params();worker._migrate_initial_selection(p,sources)
  assert p.get('RoadContextMapSelection')==['HU','IT','SI']
  p.put('RoadContextMapSelection',[]);worker._migrate_initial_selection(p,sources)
  assert p.get('RoadContextMapSelection')==[]


def test_preloaded_map_is_verified_and_adopted_without_download(tmp_path,monkeypatch):
  monkeypatch.setattr(worker,'MAP_DIR',tmp_path)
  source=MapSource('HU','Hungary','https://download.geofabrik.de/europe/hungary-latest.osm.pbf',('HU',))
  path=tmp_path/'HU.sqlite'
  write_database(path,[Road('1',[(47.,19.),(47.001,19.)],{'highway':'primary','maxspeed':'50'},'HU')],{'schema':1,'version':'v1','source':source.url,'snapshot_epoch':1})
  p=Params();installed={}
  assert worker._adopt_existing(p,source,installed)
  assert record_matches(path,installed['HU'])
  monkeypatch.setattr(worker,'_read_db_sha',lambda _:pytest.fail('unchanged verified map must not be rehashed every loop'))
  assert worker._adopt_existing(p,source,installed)


def test_deselection_removes_only_chosen_country(tmp_path,monkeypatch):
  monkeypatch.setattr(worker,'MAP_DIR',tmp_path);monkeypatch.setattr(worker,'IMPORT_DIR',tmp_path/'.imports')
  for cc in ['HU','SI','IT']:
    (tmp_path/(cc+'.sqlite')).write_bytes(b'map');(tmp_path/(cc+'.verified.json')).write_text('{}')
  sources=[MapSource(cc,cc,'https://download.geofabrik.de/europe/'+cc.lower()+'.osm.pbf',(cc,)) for cc in ['HU','SI','IT']]
  p=Params();installed={cc:{} for cc in ['HU','SI','IT']}
  worker._remove_deselected(p,sources,{'HU','IT'},installed)
  assert not (tmp_path/'SI.sqlite').exists() and not (tmp_path/'SI.verified.json').exists()
  assert (tmp_path/'HU.sqlite').exists() and (tmp_path/'IT.sqlite').exists()


def test_bundled_catalog_contains_current_installed_and_european_territories():
  path=Path(worker.__file__).parent/'data/europe_sources.json'
  sources=worker._read_sources(Params())
  ids={s.package_id for s in sources}
  assert {'HU','SI','IT','DE','FR','GB','XK','IM','GUERNSEY-JERSEY'}<=ids
  assert len(ids)==len(json.loads(path.read_text())['sources'])


@pytest.mark.parametrize("field,value",[("roads","1"),("roads",None),("bytes",[]),("mtime_ns",{}),("sha256",None)])
def test_bad_registry_fields_do_not_crash_menu(tmp_path,field,value):
  path=tmp_path/'HU.sqlite';path.write_bytes(b'map');record=proof(path);record[field]=value
  assert not record_matches(path,record)
  assert country_state('HU',{'HU'},{'HU':record},{'id':'HU','state':[]},tmp_path)=='pending'


def test_cellular_or_onroad_cannot_download(monkeypatch):
  import sys
  from types import SimpleNamespace
  fake=SimpleNamespace(log=SimpleNamespace(DeviceState=SimpleNamespace(NetworkType=SimpleNamespace(wifi=7))))
  monkeypatch.setitem(sys.modules,'cereal',fake)
  class Flags:
    def __init__(self,onroad):self.onroad=onroad
    def get_bool(self,key):return self.onroad if key=='IsOnroad' else not self.onroad
  class Device:
    valid={'deviceState':True};alive={'deviceState':True};network=7
    def __getitem__(self,key):return SimpleNamespace(networkType=self.network)
  sm=Device();assert worker._network_ready(Flags(False),sm)
  sm.network=1;assert not worker._network_ready(Flags(False),sm)
  sm.network=7;assert not worker._network_ready(Flags(True),sm)
  sm.alive={'deviceState':False};assert not worker._network_ready(Flags(False),sm)
