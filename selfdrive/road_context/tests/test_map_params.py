import tempfile
import unittest
from pathlib import Path
from openpilot.selfdrive.road_context.map_params import country_map_status, set_download_status

class Params:
  def __init__(self): self.data={}
  def get(self,key): return self.data.get(key)
  def put(self,key,value): self.data[key]=value

class MapParamsTests(unittest.TestCase):
  def test_verified_map_requires_file_record_and_active_configuration(self):
    with tempfile.TemporaryDirectory() as tmp:
      directory=Path(tmp); db=directory/"HU.sqlite"; db.write_bytes(b"map")
      installed={"HU":{"id":"HU","path":str(db),"bytes":3,"roads":42,"integrity":"ok","sha256":"a"*64}}
      state=country_map_status("HU",selected=True,installed=installed,statuses={},latest_status={},
        configured_path=str(directory),display_enabled=True,map_dir=str(directory))
      self.assertEqual(("active",42,""),state)
      state=country_map_status("HU",selected=True,installed=installed,statuses={},latest_status={},
        configured_path="",display_enabled=True,map_dir=str(directory))
      self.assertEqual(("verified_inactive",42,""),state)

  def test_file_without_verified_record_is_not_active(self):
    with tempfile.TemporaryDirectory() as tmp:
      directory=Path(tmp); (directory/"HU.sqlite").write_bytes(b"map")
      state=country_map_status("HU",selected=True,installed={},statuses={},latest_status={},
        configured_path=str(directory),display_enabled=True,map_dir=str(directory))
      self.assertEqual(("unverified",0,""),state)

  def test_country_specific_and_legacy_building_status(self):
    latest={"id":"IT","state":"building"}
    self.assertEqual(("building",0,""),country_map_status("IT",selected=True,installed={},statuses={},
      latest_status=latest,configured_path="",display_enabled=True,map_dir="/maps"))
    self.assertEqual(("queued",0,""),country_map_status("SI",selected=True,installed={},statuses={},
      latest_status=latest,configured_path="",display_enabled=True,map_dir="/maps"))

  def test_download_status_keeps_other_country_states(self):
    params=Params()
    set_download_status(params,"HU","complete",roads=4)
    set_download_status(params,"IT","building")
    self.assertEqual("building",params.data["RoadContextMapDownloadStatus"]["state"])
    self.assertEqual("complete",params.data["RoadContextMapDownloadStatus"]["countries"]["HU"]["state"])
    self.assertEqual("building",params.data["RoadContextMapDownloadStatus"]["countries"]["IT"]["state"])

if __name__ == "__main__":
  unittest.main()
