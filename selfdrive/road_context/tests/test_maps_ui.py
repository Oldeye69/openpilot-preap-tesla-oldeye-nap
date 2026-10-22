"""Construct the real ARM64 widget tree without opening a second display."""
import sys
import json
import hashlib
from types import SimpleNamespace
import pytest
if sys.platform == 'win32':
  pytest.skip('Native UI construction runs on the comma',allow_module_level=True)
import pyray as rl
from openpilot.selfdrive.ui.layouts.settings import nap_maps as menu
from openpilot.selfdrive.road_context.map_state import COLORS

class Params:
  def __init__(self,values):self.values=values
  def get(self,key):return self.values.get(key)
  def put(self,key,value):self.values[key]=value

@pytest.fixture
def layout(tmp_path,monkeypatch):
  rows={}
  for cc in ['HU','SI','IT']:
    p=tmp_path/(cc+'.sqlite');p.write_bytes(b'verified')
    rows[cc]=dict(bytes=p.stat().st_size,mtime_ns=p.stat().st_mtime_ns,sha256=hashlib.sha256(p.read_bytes()).hexdigest(),roads=1,integrity='ok')
  params=Params({'RoadContextMapSelection':['HU','SI','IT'],'RoadContextMapInstalled':rows})
  monkeypatch.setattr(menu,'Params',lambda:params);monkeypatch.setattr(menu,'MAP_DIR',tmp_path)
  monkeypatch.setattr(menu.gui_app,'font',lambda *_:rl.ffi.new('Font *')[0])
  from openpilot.system.ui.widgets import list_view
  monkeypatch.setattr(list_view,'measure_text_cached',lambda *_:rl.Vector2(250,50))
  monkeypatch.setattr(menu,'can_select_maps',lambda:True)
  return menu.NAPMapsLayout(),params


def test_real_widget_opens_all_countries_with_three_green_rows(layout):
  widget,params=layout
  assert len(widget._rows)==48
  for cc in ['HU','SI','IT']:
    c=widget._colour(cc);assert (c.r,c.g,c.b,c.a)==COLORS['installed']
    assert widget._rows[cc].action_item.get_state()


def test_row_changes_live_without_reopening_or_losing_scroll(layout):
  widget,params=layout;scroller=widget._scroller
  params.values['RoadContextMapDownloadStatus']={'id':'IT','state':'building'}
  widget._next_refresh=0;widget._refresh()
  assert widget._scroller is scroller
  c=widget._rows['IT']._title_color();assert (c.r,c.g,c.b,c.a)==COLORS['pending']
  params.values['RoadContextMapDownloadStatus']={'id':'IT','state':'complete'}
  widget._next_refresh=0;widget._refresh()
  c=widget._rows['IT']._title_color();assert (c.r,c.g,c.b,c.a)==COLORS['installed']


def test_selection_keeps_existing_countries(layout):
  widget,params=layout
  widget._set_selected('DE',True)
  assert set(params.values['RoadContextMapSelection'])=={'HU','SI','IT','DE'}
  widget._set_selected('SI',False)
  assert set(params.values['RoadContextMapSelection'])=={'HU','IT','DE'}


def test_parent_nap_contains_maps_entry_without_eager_widget_construction():
  from pathlib import Path
  import ast
  tree=ast.parse((Path(menu.__file__).with_name('nap.py')).read_text())
  function=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_on_open_maps')
  assert any(isinstance(n,ast.ImportFrom) and n.module.endswith('.nap_maps') for n in function.body)



def test_actual_list_renderer_colours_the_country_title_and_keeps_other_items_white(layout,monkeypatch):
  from openpilot.system.ui.widgets import list_view
  widget,params=layout;drawn=[]
  monkeypatch.setattr(list_view,'measure_text_cached',lambda *_:rl.Vector2(250,50))
  monkeypatch.setattr(rl,'draw_text_ex',lambda font,text,pos,size,spacing,color:drawn.append((text,(color.r,color.g,color.b,color.a) if hasattr(color,'r') else tuple(color))))
  row=widget._rows['HU'];row.set_parent_rect(rl.Rectangle(0,0,1600,1000));row.set_rect(rl.Rectangle(0,0,1600,170));monkeypatch.setattr(row.action_item,'render',lambda _:False)
  row._render(row.rect);assert drawn[-1][1]==COLORS['installed']
  widget._progress={'id':'HU','state':'building'};row._render(row.rect);assert drawn[-1][1]==COLORS['pending']
  other=list_view.simple_item('Default');other.set_parent_rect(rl.Rectangle(0,0,1600,1000));other.set_rect(rl.Rectangle(0,0,1600,170));other._render(other.rect)
  assert drawn[-1][1]==COLORS['available']


def test_selection_requires_stationary_live_vehicle_data(monkeypatch):
  class SM:
    valid={'carState':True};alive={'carState':True};speed=0.
    def __getitem__(self,_):return SimpleNamespace(vEgo=self.speed)
  sm=SM();monkeypatch.setattr(menu,'ui_state',SimpleNamespace(is_offroad=lambda:False,sm=sm))
  assert menu.can_select_maps()
  sm.speed=1.;assert not menu.can_select_maps()
  sm.speed=0.;sm.alive={'carState':False};assert not menu.can_select_maps()



def test_legend_uses_full_scroller_width():
  legend=menu.MapLegend();legend.set_parent_rect(rl.Rectangle(0,0,1600,900))
  assert legend.rect.width==1600 and legend.rect.height==75
