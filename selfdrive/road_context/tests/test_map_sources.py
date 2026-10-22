import hashlib
import io
import json

import pytest

from openpilot.selfdrive.road_context.map_sources import MapSource, download_pbf, parse_geofabrik_index
from openpilot.selfdrive.road_context.map_params import load_json, put_if_changed, set_download_status
from openpilot.selfdrive.road_context.pbf_import import _wheel_filename, import_pbf


def feature(parent, country, ident, name, url):
  return {"type": "Feature", "properties": {"parent": parent, "iso3166-1:alpha2": [country],
          "id": ident, "name": name, "urls": {"pbf": url}}}


def test_geofabrik_index_only_accepts_direct_european_country_extracts():
  data = json.dumps({"type": "FeatureCollection", "features": [
    feature("europe", "HU", "europe/hungary", "Hungary", "https://download.geofabrik.de/europe/hungary-latest.osm.pbf"),
    feature("europe/germany", "DE", "europe/germany/berlin", "Berlin", "https://download.geofabrik.de/europe/germany/berlin-latest.osm.pbf"),
    feature("asia", "JP", "asia/japan", "Japan", "https://download.geofabrik.de/asia/japan-latest.osm.pbf"),
  ]}).encode()
  sources = parse_geofabrik_index(data)
  assert [(s.package_id, s.countries) for s in sources] == [("HU", ("HU",))]


@pytest.mark.parametrize("url", ["http://download.geofabrik.de/a.osm.pbf",
                                   "https://evil.example/a.osm.pbf",
                                   "https://download.geofabrik.de/a.osm.pbf?redirect=x"])
def test_index_rejects_non_geofabrik_source_url(url):
  with pytest.raises(ValueError):
    parse_geofabrik_index(json.dumps({"type": "FeatureCollection", "features": [
      feature("europe", "HU", "europe/hungary", "Hungary", url),
    ]}).encode())


def test_map_params_use_native_json_objects():
  class Params:
    def __init__(self):
      self.values = {}
    def get(self, key):
      return self.values.get(key)
    def put(self, key, value):
      assert isinstance(value, (dict, list))
      self.values[key] = value

  params = Params()
  catalog = {"sources": [{"package_id": "HU"}]}
  put_if_changed(params, "RoadContextMapCatalog", catalog)
  assert load_json(params, "RoadContextMapCatalog", {}) == catalog
  put_if_changed(params, "RoadContextMapSelection", ["HU"])
  assert load_json(params, "RoadContextMapSelection", []) == ["HU"]
  set_download_status(params, "HU", "complete", roads=1)
  assert load_json(params, "RoadContextMapDownloadStatus", {})["state"] == "complete"


def test_pinned_osmium_wheel_uses_full_abi_filename():
  from openpilot.selfdrive.road_context.pbf_import import OSMIUM_WHEELS
  assert _wheel_filename(OSMIUM_WHEELS[(3, 12)][0]) == (
    "osmium-4.3.2-cp312-cp312-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl"
  )


class Response(io.BytesIO):
  status = 200
  def __init__(self, data, url, *, status=200, headers=None, chunk_size=None):
    super().__init__(data)
    self._url = url
    self.status = status
    self.headers = headers or {}
    self.chunk_size = chunk_size

  def read(self, size=-1):
    if self.chunk_size is not None and size >= 0:
      size = min(size, self.chunk_size)
    return super().read(size)

  def geturl(self):
    return self._url

  def __enter__(self):
    return self

  def __exit__(self, *_):
    self.close()


def test_download_pbf_verifies_hash_and_promotes_staged_file(tmp_path):
  raw = b"tiny test PBF payload"
  source = MapSource("HU", "Hungary", "https://download.geofabrik.de/europe/hungary-latest.osm.pbf", ("HU",))
  result = download_pbf(source, tmp_path, timeout=1,
                        opener=lambda *_args, **_kwargs: Response(raw, source.url))
  assert result["sha256"] == hashlib.sha256(raw).hexdigest()
  assert (tmp_path / "HU.osm.pbf").read_bytes() == raw
  assert not (tmp_path / "HU.osm.pbf.part").exists()
  reused = download_pbf(source, tmp_path, timeout=1, opener=lambda *_args, **_kwargs: pytest.fail("complete source should be reused"))
  assert reused["sha256"] == result["sha256"]


def test_pbf_download_resumes_partial_bytes_with_http_validator(tmp_path):
  raw = b"abcdef"
  source = MapSource("HU", "Hungary", "https://download.geofabrik.de/europe/hungary-latest.osm.pbf", ("HU",))
  responses = [
    Response(raw, source.url, headers={"Content-Length": "6", "ETag": '"v1"'}, chunk_size=3),
    Response(raw[3:], source.url, status=206,
             headers={"Content-Range": "bytes 3-5/6", "Content-Length": "3", "ETag": '"v1"'}, chunk_size=3),
  ]
  def cancel_after_first_chunk():
    cancel_after_first_chunk.calls += 1
    return cancel_after_first_chunk.calls > 1
  cancel_after_first_chunk.calls = 0
  with pytest.raises(InterruptedError):
    download_pbf(source, tmp_path, expected_size=6, expected_etag='"v1"',
                 opener=lambda *_args, **_kwargs: responses.pop(0), should_cancel=cancel_after_first_chunk)
  request_seen = []
  def resume_opener(request, **_kwargs):
    request_seen.append(request)
    return responses.pop(0)
  result = download_pbf(source, tmp_path, expected_size=6, expected_etag='"v1"', opener=resume_opener)
  assert request_seen[0].headers.get("Range") == "bytes=3-"
  assert result["sha256"] == hashlib.sha256(raw).hexdigest()
  assert (tmp_path / "HU.osm.pbf").read_bytes() == raw


def test_pbf_import_streams_to_schema_and_does_not_invent_country(tmp_path, monkeypatch):
  class Location:
    def __init__(self, lat, lon):
      self.lat, self.lon = lat, lon
    def valid(self):
      return True

  class Node:
    def __init__(self, lat, lon):
      self.location = Location(lat, lon)

  class Tag:
    def __init__(self, key, value):
      self.k, self.v = key, value

  class Way:
    id = 42
    tags = [Tag("highway", "primary"), Tag("maxspeed", "50")]
    nodes = [Node(47.0, 19.0), Node(47.01, 19.01)]

  class Handler:
    def __init__(self):
      pass
    def apply_file(self, _path, *, locations, idx):
      assert locations is True and idx.startswith("sparse_file_array,")
      self.way(Way())

  import types
  monkeypatch.setitem(__import__("sys").modules, "osmium", types.SimpleNamespace(SimpleHandler=Handler))
  source = MapSource("HU", "Hungary", "https://download.geofabrik.de/europe/hungary-latest.osm.pbf", ("HU",))
  pbf = tmp_path / "HU.osm.pbf"
  pbf.write_bytes(b"fixture")
  building = tmp_path / "HU.building.sqlite"
  result = import_pbf(pbf, building, source=source,
                      source_info={"last_modified": "Wed, 21 Oct 2026 00:00:00 GMT", "etag": '"e1"', "snapshot_epoch": 1},
                      package_dir=tmp_path / "packages", node_index_path=tmp_path / "HU.nodes")
  assert result["integrity"] == "ok" and result["roads"] == 1
  import sqlite3
  db = sqlite3.connect(building)
  metadata = json.loads(db.execute("SELECT data FROM metadata").fetchone()[0])
  row = db.execute("SELECT road_id,country FROM roads").fetchone()
  assert metadata["country_context"] == "UNVERIFIED_EXTRACT"
  assert row == ("42", "")
  assert db.execute("SELECT COUNT(*) FROM bounds").fetchone()[0] == 1
  db.close()
