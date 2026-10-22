"""Wi-Fi/offroad Geofabrik downloader and on-device SQLite map builder."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from cereal import log, messaging
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.road_context.map_params import (
  load_json as _load_json, put_if_changed as _put_if_changed, set_download_status as _set_status,
)
from openpilot.selfdrive.road_context.map_sources import (
  COUNTRY_RE, ID_RE, INDEX_URL, MapSource, _source_url, download_pbf, fetch_index, parse_geofabrik_index, probe_source,
)
from openpilot.selfdrive.road_context.pbf_import import import_pbf


MAP_DIR = Path("/data/road-context/maps")
IMPORT_DIR = MAP_DIR / ".imports"
PACKAGE_DIR = Path("/data/road-context/python-packages")
MIN_FREE_BYTES = 768 * 1024 * 1024


def _source_from_json(item) -> MapSource | None:
  if not isinstance(item, dict):
    return None
  try:
    package_id = str(item["package_id"])
    name = str(item["name"])[:120]
    url = _source_url(str(item["url"]))
    countries = tuple(sorted(set(str(code).upper() for code in item["countries"])))
    if not ID_RE.fullmatch(package_id) or not countries or any(not COUNTRY_RE.fullmatch(code) for code in countries):
      return None
    return MapSource(package_id, name, url, countries)
  except (KeyError, TypeError, ValueError, IndexError):
    return None


def _read_sources(params) -> list[MapSource]:
  catalog = _load_json(params, "RoadContextMapCatalog", {})
  if not isinstance(catalog, dict):
    return []
  raw_sources = catalog.get("sources", [])
  if not isinstance(raw_sources, list) or len(raw_sources) > 500:
    return []
  sources = [_source_from_json(item) for item in raw_sources]
  return [source for source in sources if source is not None]


def _known_map_ids(sources: list[MapSource]) -> set[str]:
  return {source.package_id for source in sources}


def _migrate_initial_selection(params, sources):
  valid_ids = _known_map_ids(sources)
  saved = _load_json(params, "RoadContextMapSelection", None)
  if isinstance(saved, list):
    return
  else:
    present = {path.stem for path in MAP_DIR.glob("*.sqlite") if path.is_file() and not path.is_symlink()}
    selected = sorted(present & valid_ids)
  params.put("RoadContextMapSelection", selected)


def _safe_package_path(directory: Path, package_id: str, suffix: str) -> Path:
  if not package_id or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in package_id):
    raise ValueError("refusing invalid map id for filesystem operation")
  path = directory / f"{package_id}{suffix}"
  if path.is_symlink():
    raise ValueError(f"refusing to remove symlink {path.name}")
  return path


def _remove_deselected(params, sources, selected, installed):
  MAP_DIR.mkdir(parents=True, exist_ok=True)
  IMPORT_DIR.mkdir(parents=True, exist_ok=True)
  if MAP_DIR.is_symlink() or IMPORT_DIR.is_symlink():
    raise ValueError("map storage path must not be a symlink")
  installed_before = json.dumps(installed, sort_keys=True, separators=(",", ":"))
  removed_any = False
  deselected_ids = {source.package_id for source in sources if source.package_id not in selected}
  for source in sources:
    package_id = source.package_id
    if package_id in selected:
      continue
    for suffix in (".sqlite", ".sqlite-journal", ".sqlite-wal", ".sqlite-shm"):
      path = _safe_package_path(MAP_DIR, package_id, suffix)
      if path.exists():
        path.unlink()
        removed_any = True
    for suffix in (".osm.pbf", ".osm.pbf.json", ".osm.pbf.json.tmp", ".osm.pbf.part",
                   ".osm.pbf.part.json", ".osm.pbf.part.json.tmp", ".nodes", ".building.sqlite",
                   ".building.sqlite-journal", ".building.sqlite-wal", ".building.sqlite-shm"):
      path = _safe_package_path(IMPORT_DIR, package_id, suffix)
      if path.exists():
        path.unlink()
        removed_any = True
    removed_any |= package_id in installed
    installed.pop(package_id, None)
  installed_after = json.dumps(installed, sort_keys=True, separators=(",", ":"))
  if installed_after != installed_before:
    _put_if_changed(params, "RoadContextMapInstalled", installed)
  if removed_any:
    status = _load_json(params, "RoadContextMapDownloadStatus", {})
    if isinstance(status, dict) and status.get("id") in deselected_ids:
      _set_status(params, status["id"], "removed")
  if not any(path.is_file() and not path.name.endswith(".building.sqlite") for path in MAP_DIR.glob("*.sqlite")):
    if params.get("RoadContextMapPath"):
      params.remove("RoadContextMapPath")


def _source_is_current(database: Path, source: MapSource, remote: dict) -> bool:
  if not database.is_file() or database.is_symlink():
    return False
  try:
    import sqlite3
    db = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
      metadata = json.loads(db.execute("SELECT data FROM metadata LIMIT 1").fetchone()[0])
    finally:
      db.close()
    if metadata.get("package_id") != source.package_id or metadata.get("source_url") != source.url:
      return False
    last_modified, etag = remote.get("last_modified"), remote.get("etag")
    if last_modified and metadata.get("source_last_modified") != last_modified:
      return False
    if etag and metadata.get("source_etag") != etag:
      return False
    # Without validators, trust the already-installed verified content rather
    # than redownloading a multi-GB extract every daemon cycle.
    return bool(metadata.get("source_sha256"))
  except (OSError, ValueError, TypeError, IndexError, KeyError):
    return False


def _free_space_ok(source_size):
  free = shutil.disk_usage(MAP_DIR.parent).free
  # PBF, sparse node index and SQLite can coexist during the atomic rebuild.
  needed = max(MIN_FREE_BYTES, (source_size or 0) * 5 + MIN_FREE_BYTES)
  if free < needed:
    raise OSError(f"not enough free space: need about {needed // (1024 ** 3)} GiB")


def _refresh_index(params):
  document, last_modified = fetch_index()
  sources = parse_geofabrik_index(document)
  params.put("RoadContextMapCatalog", {
    "schema_version": 1, "source": INDEX_URL, "source_last_modified": last_modified,
    "license": "ODbL-1.0", "attribution": "© OpenStreetMap contributors",
    "sources": [source.to_json() for source in sources],
  })
  return sources


def _process_source(params, source, installed, remote):
  MAP_DIR.mkdir(parents=True, exist_ok=True)
  IMPORT_DIR.mkdir(parents=True, exist_ok=True)
  if MAP_DIR.is_symlink() or IMPORT_DIR.is_symlink():
    raise ValueError("map storage path must not be a symlink")
  final = _safe_package_path(MAP_DIR, source.package_id, ".sqlite")
  if _source_is_current(final, source, remote):
    record = installed.get(source.package_id)
    if not isinstance(record, dict) or not record.get("sha256"):
      import sqlite3
      db = sqlite3.connect(final.resolve().as_uri() + "?mode=ro", uri=True)
      try:
        metadata = json.loads(db.execute("SELECT data FROM metadata LIMIT 1").fetchone()[0])
        roads = db.execute("SELECT COUNT(*) FROM roads").fetchone()[0]
      finally:
        db.close()
      installed[source.package_id] = {
        "id": source.package_id, "path": str(final), "sha256": _read_db_sha(final),
        "bytes": final.stat().st_size, "roads": roads, "integrity": "ok",
        "source_sha256": metadata.get("source_sha256"),
        "source_last_modified": metadata.get("source_last_modified"),
        "source_etag": metadata.get("source_etag"), "verified_at": time.time(),
      }
    _put_if_changed(params, "RoadContextMapPath", str(MAP_DIR))
    return False
  _free_space_ok(remote.get("bytes"))
  _set_status(params, source.package_id, "downloading", bytes_total=remote.get("bytes"))
  is_selected = lambda: source.package_id in _selected_ids(params, {source.package_id})
  download = download_pbf(source, IMPORT_DIR, should_cancel=lambda: not is_selected(),
                          expected_size=remote.get("bytes"), expected_etag=remote.get("etag"),
                          expected_last_modified=remote.get("last_modified"))
  download["last_modified"] = download.get("last_modified") or remote.get("last_modified")
  download["etag"] = download.get("etag") or remote.get("etag")
  download["snapshot_epoch"] = download.get("snapshot_epoch") or remote.get("snapshot_epoch")
  _set_status(params, source.package_id, "building", source_bytes=download["bytes"])
  building = _safe_package_path(IMPORT_DIR, source.package_id, ".building.sqlite")
  node_index = _safe_package_path(IMPORT_DIR, source.package_id, ".nodes")
  result = import_pbf(IMPORT_DIR / f"{source.package_id}.osm.pbf", building,
                      source=source, source_info=download, package_dir=PACKAGE_DIR,
                      node_index_path=node_index, should_cancel=lambda: not is_selected())
  # Preserve a working database until its replacement passes full validation.
  _validate_built_database(building, source, result)
  os.replace(building, final)
  (IMPORT_DIR / f"{source.package_id}.osm.pbf").unlink(missing_ok=True)
  (IMPORT_DIR / f"{source.package_id}.osm.pbf.json").unlink(missing_ok=True)
  for suffix in ("-journal", "-wal", "-shm"):
    Path(str(building) + suffix).unlink(missing_ok=True)
  installed[source.package_id] = {
    "id": source.package_id, "path": str(final), "bytes": result["bytes"], "sha256": result["sha256"],
    "roads": result["roads"], "integrity": result["integrity"],
    "source_sha256": download["sha256"], "source_last_modified": download.get("last_modified"),
    "source_etag": download.get("etag"), "verified_at": time.time(),
  }
  _put_if_changed(params, "RoadContextMapPath", str(MAP_DIR))
  return True


def _read_db_sha(path):
  import hashlib
  digest = hashlib.sha256()
  with path.open("rb") as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b""):
      digest.update(block)
  return digest.hexdigest()


def _validate_built_database(path, source, result):
  import sqlite3
  db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
  try:
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
      raise ValueError("built map SQLite integrity check failed")
    metadata = json.loads(db.execute("SELECT data FROM metadata LIMIT 1").fetchone()[0])
    if metadata.get("schema") != 1 or metadata.get("package_id") != source.package_id:
      raise ValueError("built map metadata does not match country")
    if metadata.get("source_sha256") != result["metadata"].get("source_sha256"):
      raise ValueError("built map source fingerprint changed")
    count = db.execute("SELECT COUNT(*) FROM roads").fetchone()[0]
    if count != result["roads"] or count <= 0:
      raise ValueError("built map road count mismatch")
  finally:
    db.close()


def _selected_ids(params, valid_ids):
  raw = _load_json(params, "RoadContextMapSelection", [])
  return {item for item in raw if isinstance(item, str) and item in valid_ids} if isinstance(raw, list) else set()


def main():
  params = Params()
  sm = messaging.SubMaster(["deviceState"])
  sources = _read_sources(params)
  last_catalog = 0.0
  probed_sources = {}
  while True:
    sm.update(1000)
    now = time.monotonic()
    on_wifi = (sm.valid["deviceState"] and
               sm["deviceState"].networkType == log.DeviceState.NetworkType.wifi)
    if on_wifi and (not sources or now - last_catalog >= 6 * 60 * 60):
      try:
        sources = _refresh_index(params)
        last_catalog = now
        _migrate_initial_selection(params, sources)
      except Exception:
        cloudlog.exception("NAP Geofabrik country index refresh failed")
        time.sleep(10)
        sources = _read_sources(params)

    if sources:
      _migrate_initial_selection(params, sources)
      selected = _load_json(params, "RoadContextMapSelection", [])
      if not isinstance(selected, list):
        selected = []
      valid_ids = {source.package_id for source in sources}
      selected = {item for item in selected if isinstance(item, str) and item in valid_ids}
      installed = _load_json(params, "RoadContextMapInstalled", {})
      if not isinstance(installed, dict):
        installed = {}
      try:
        _remove_deselected(params, sources, selected, installed)
      except Exception:
        cloudlog.exception("NAP deselected map cleanup failed")
      if on_wifi:
        for source in sources:
          if source.package_id not in selected:
            continue
          try:
            cached = probed_sources.get(source.package_id)
            if cached is None or now - cached[0] > 6 * 60 * 60:
              cached = (now, probe_source(source))
              probed_sources[source.package_id] = cached
            before = json.dumps(installed.get(source.package_id), sort_keys=True, separators=(",", ":"))
            changed = _process_source(params, source, installed, cached[1])
            after = json.dumps(installed.get(source.package_id), sort_keys=True, separators=(",", ":"))
            if before != after:
              params.put("RoadContextMapInstalled", installed)
            if any(path.is_file() and not path.is_symlink() for path in MAP_DIR.glob("*.sqlite")):
              _put_if_changed(params, "RoadContextMapPath", str(MAP_DIR))
            if changed:
              _set_status(params, source.package_id, "complete", **installed[source.package_id])
          except InterruptedError:
            _set_status(params, source.package_id, "cancelled")
            break
          except Exception as e:
            cloudlog.exception("NAP on-device OSM import failed: %s", source.package_id)
            _set_status(params, source.package_id, "error", message=str(e)[:180])
            time.sleep(10)
            break
    time.sleep(2)


if __name__ == "__main__":
  main()
