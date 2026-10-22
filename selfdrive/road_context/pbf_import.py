"""Incremental, disk-indexed OSM PBF to NAP road-context SQLite importer."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path


SCHEMA_VERSION = 1
OSMIUM_VERSION = "4.3.2"
OSMIUM_WHEELS = {
  (3, 11): (
    "https://files.pythonhosted.org/packages/74/e7/dd160a096c3f02519b30483de547e44df604afc3d1c9339d6c86d8902c9b/"
    "osmium-4.3.2-cp311-cp311-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl",
    "1c45037a8fd04fcc8481387853d411d3a1e1c93b7c6da7be201159c4a2526134",
  ),
  (3, 12): (
    "https://files.pythonhosted.org/packages/98/a7/9bb3a6d9870bfee34075a0d54c7b76f81eeb408bc3d5510d4ef0098608bf/"
    "osmium-4.3.2-cp312-cp312-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl",
    "fb7ac18aba1b3913904c34993a40086e4fa56e9f4debe84c9f5a1caeddc550d4",
  ),
}
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


def _sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with Path(path).open("rb") as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b""):
      digest.update(block)
  return digest.hexdigest()


def _wheel_filename(url: str) -> str:
  from urllib.parse import urlparse
  filename = Path(urlparse(url).path).name
  if not filename.endswith(".whl") or "/" in filename or "\\" in filename:
    raise ValueError("invalid pinned osmium wheel filename")
  return filename


def ensure_osmium(package_dir: Path) -> None:
  """Install only the pinned CPython 3.11 ARM64 binary wheel on demand."""
  package_dir = Path(package_dir)
  package_dir.mkdir(parents=True, exist_ok=True)
  if package_dir.is_symlink():
    raise ValueError("Python package directory must not be a symlink")
  if str(package_dir) not in sys.path:
    sys.path.insert(0, str(package_dir))
  try:
    import osmium  # noqa: F401
    return
  except ImportError:
    pass

  import platform
  import subprocess
  wheel_spec = OSMIUM_WHEELS.get(sys.version_info[:2])
  if wheel_spec is None or platform.machine().lower() not in ("aarch64", "arm64"):
    raise RuntimeError("OSM import requires a verified CPython 3.11/3.12 ARM64 runtime")
  wheel_url, wheel_digest = wheel_spec
  wheel = package_dir / _wheel_filename(wheel_url)
  # Early NAP builds used an invalid shortened filename (cp12 instead of
  # cp312). Reuse it only after verifying the exact pinned digest.
  legacy_wheel = package_dir / f"osmium-{OSMIUM_VERSION}-cp{sys.version_info.minor}-aarch64.whl"
  if not wheel.exists() and legacy_wheel.is_file() and not legacy_wheel.is_symlink():
    if _sha256(legacy_wheel) == wheel_digest:
      os.replace(legacy_wheel, wheel)
    else:
      legacy_wheel.unlink()
  partial = wheel.with_suffix(wheel.suffix + ".part")
  from urllib.parse import urlparse
  from urllib.request import Request, urlopen
  if urlparse(wheel_url).hostname != "files.pythonhosted.org":
    raise RuntimeError("unexpected PyPI wheel host")
  request = Request(wheel_url, headers={"User-Agent": "NAP-on-device-map-import/1", "Accept-Encoding": "identity"})
  with urlopen(request, timeout=30) as response, partial.open("wb") as output:
    if response.status != 200 or urlparse(response.geturl()).hostname != "files.pythonhosted.org":
      raise OSError("pinned PyPI wheel download failed")
    while block := response.read(1024 * 1024):
      if output.tell() + len(block) > 5 * 1024 * 1024:
        raise ValueError("pinned pyosmium wheel exceeds size limit")
      output.write(block)
    output.flush()
    os.fsync(output.fileno())
  if _sha256(partial) != wheel_digest:
    partial.unlink(missing_ok=True)
    raise ValueError("pinned pyosmium wheel SHA-256 mismatch")
  os.replace(partial, wheel)
  pip_python = sys.executable
  pip_check = subprocess.run([pip_python, "-m", "pip", "--version"], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, check=False)
  if pip_check.returncode != 0:
    candidate = Path("/usr/local/venv/bin/python")
    if not candidate.is_file():
      raise RuntimeError("pip is unavailable in the active Comma Python and bundled venv")
    version = subprocess.check_output([str(candidate), "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"], text=True).strip()
    if version != f"{sys.version_info.major}.{sys.version_info.minor}":
      raise RuntimeError("bundled pip Python does not match the active Comma Python ABI")
    pip_python = str(candidate)
  subprocess.run([pip_python, "-m", "pip", "install", "--no-deps", "--no-index", "--target",
                  str(package_dir), str(wheel)], check=True, timeout=180)
  import osmium  # noqa: F401


def _remove_sqlite_family(path: Path) -> None:
  for suffix in ("", "-journal", "-wal", "-shm"):
    candidate = Path(str(path) + suffix)
    if candidate.is_symlink():
      raise ValueError(f"refusing symlink in map import output: {candidate.name}")
    candidate.unlink(missing_ok=True)


def import_pbf(pbf_path: Path, building_path: Path, *, source, source_info: dict,
               package_dir: Path, node_index_path: Path, should_cancel=None) -> dict:
  """Write ways directly to SQLite in bounded batches; nodes stay disk indexed."""
  import_pbf_path = Path(pbf_path)
  building_path = Path(building_path)
  node_index_path = Path(node_index_path)
  if building_path.exists() or building_path.is_symlink():
    _remove_sqlite_family(building_path)
  if node_index_path.is_symlink():
    raise ValueError("refusing symlink as OSM node index")
  node_index_path.unlink(missing_ok=True)
  ensure_osmium(package_dir)
  import osmium

  countries = set(source.countries)
  if not countries or any(not COUNTRY_RE.fullmatch(code) for code in countries):
    raise ValueError("source country metadata is invalid")
  source_digest = _sha256(import_pbf_path)
  metadata = {
    "schema": SCHEMA_VERSION,
    "version": source_digest,
    "source_sha256": source_digest,
    "source_url": source.url,
    "source_bytes": import_pbf_path.stat().st_size,
    "source_last_modified": source_info.get("last_modified"),
    "source_etag": source_info.get("etag"),
    "snapshot_epoch": source_info.get("snapshot_epoch"),
    "package_id": source.package_id,
    "countries": sorted(countries),
    "country": source.countries[0] if len(source.countries) == 1 else "",
    "country_context": "UNVERIFIED_EXTRACT",
    "license": "ODbL-1.0",
    "attribution": "OpenStreetMap contributors",
  }
  db = sqlite3.connect(building_path)
  db.execute("PRAGMA journal_mode=DELETE")
  db.execute("PRAGMA synchronous=FULL")
  db.execute("PRAGMA cache_size=-65536")
  db.executescript("""
    CREATE TABLE metadata(data TEXT NOT NULL);
    CREATE TABLE roads(id INTEGER PRIMARY KEY, road_id TEXT NOT NULL, geometry TEXT NOT NULL,
                       tags TEXT NOT NULL, country TEXT NOT NULL);
    CREATE VIRTUAL TABLE bounds USING rtree(id,minlat,maxlat,minlon,maxlon);
  """)
  db.execute("INSERT INTO metadata VALUES(?)", (json.dumps(metadata, separators=(",", ":")),))
  db.commit()

  class RoadHandler(osmium.SimpleHandler):
    def __init__(self):
      super().__init__()
      self.row_id = 0
      self.batch = []
      self.road_count = 0

    def way(self, way):
      if should_cancel and should_cancel():
        raise InterruptedError("map selection was removed during import")
      tags = {tag.k: tag.v for tag in way.tags}
      if not tags.get("highway"):
        return
      geometry = []
      for node in way.nodes:
        if not node.location.valid():
          return
        lat, lon = node.location.lat, node.location.lon
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
          return
        geometry.append((lat, lon))
      if len(geometry) < 2:
        return
      explicit_country = tags.get("addr:country", "").strip().upper()
      country = explicit_country if explicit_country in countries else ""
      self.batch.append((self.row_id, str(way.id), json.dumps(geometry, separators=(",", ":")),
                         json.dumps(tags, separators=(",", ":")), country,
                         min(p[0] for p in geometry), max(p[0] for p in geometry),
                         min(p[1] for p in geometry), max(p[1] for p in geometry)))
      self.row_id += 1
      self.road_count += 1
      if len(self.batch) >= 1000:
        self.flush()

    def flush(self):
      if not self.batch:
        return
      db.executemany("INSERT INTO roads VALUES(?,?,?,?,?)", (row[:5] for row in self.batch))
      db.executemany("INSERT INTO bounds VALUES(?,?,?,?,?)",
                     ((row[0], *row[5:]) for row in self.batch))
      db.commit()
      self.batch.clear()

  handler = RoadHandler()
  try:
    handler.apply_file(str(import_pbf_path), locations=True,
                       idx=f"sparse_file_array,{node_index_path}")
    handler.flush()
    db.execute("CREATE INDEX roads_road_id ON roads(road_id)")
    db.commit()
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    distinct = {row[0] for row in db.execute("SELECT DISTINCT country FROM roads WHERE country != ''")}
    count = db.execute("SELECT COUNT(*) FROM roads").fetchone()[0]
    if integrity != "ok" or count <= 0 or not distinct.issubset(countries):
      raise ValueError("built SQLite failed integrity, empty-road, or country validation")
    db.close()
    return {"bytes": building_path.stat().st_size, "sha256": _sha256(building_path),
            "roads": count, "integrity": integrity, "metadata": metadata}
  except Exception:
    db.close()
    _remove_sqlite_family(building_path)
    raise
  finally:
    node_index_path.unlink(missing_ok=True)
