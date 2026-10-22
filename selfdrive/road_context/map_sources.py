"""Country-scoped, HTTPS-only raw OSM sources from Geofabrik's public index."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen


INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
SOURCE_HOST = "download.geofabrik.de"
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024 * 1024
ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


@dataclass(frozen=True)
class MapSource:
  package_id: str
  name: str
  url: str
  countries: tuple[str, ...]

  def to_json(self):
    return asdict(self) | {"countries": list(self.countries)}


def _source_url(value: str) -> str:
  parsed = urlparse(value)
  if (parsed.scheme != "https" or parsed.hostname != SOURCE_HOST or parsed.port not in (None, 443)
      or parsed.username is not None or parsed.password is not None):
    raise ValueError("OSM source must use Geofabrik HTTPS")
  if not parsed.path.endswith(".osm.pbf") or parsed.query or parsed.fragment:
    raise ValueError("invalid Geofabrik PBF URL")
  return value


def parse_geofabrik_index(document: bytes) -> list[MapSource]:
  if len(document) > MAX_INDEX_BYTES:
    raise ValueError("Geofabrik index exceeds size limit")
  payload = json.loads(document)
  if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
    raise ValueError("unsupported Geofabrik index")

  sources, codes_seen, ids_seen = [], set(), set()
  for feature in payload["features"]:
    props = feature.get("properties", {})
    # Only first-level European countries/territories. This excludes Europe-wide
    # files and subdivisions that would duplicate the parent country's roads.
    if props.get("parent") != "europe":
      continue
    codes = props.get("iso3166-1:alpha2")
    if not codes:
      codes = {"kosovo": ["XK"], "isle-of-man": ["IM"], "guernsey-jersey": ["GG", "JE"]}.get(props.get("id"))
    urls = props.get("urls", {})
    if not isinstance(codes, list) or not codes or not isinstance(urls, dict) or not urls.get("pbf"):
      continue
    countries = tuple(sorted({str(code).upper() for code in codes}))
    if any(not COUNTRY_RE.fullmatch(code) for code in countries):
      continue

    raw_id = re.sub(r"[^A-Z0-9_-]", "-", str(props.get("id", "")).upper())
    # A single-country package uses its ISO code, so the on-device database
    # name is stable even if Geofabrik changes the display name.
    package_id = countries[0] if len(countries) == 1 else raw_id
    if not ID_RE.fullmatch(package_id) or package_id in ids_seen:
      raise ValueError("duplicate or invalid European source id")
    if any(code in codes_seen for code in countries):
      raise ValueError("overlapping Geofabrik country extracts")
    sources.append(MapSource(package_id, str(props.get("name", package_id)),
                             _source_url(str(urls["pbf"])), countries))
    ids_seen.add(package_id)
    codes_seen.update(countries)

  if not sources:
    raise ValueError("Geofabrik index has no usable European country extracts")
  return sorted(sources, key=lambda source: source.name.casefold())


def fetch_index(timeout: float = 20.) -> tuple[bytes, str | None]:
  request = Request(INDEX_URL, headers={"User-Agent": "NAP-on-device-map-import/1", "Accept-Encoding": "identity"})
  with urlopen(request, timeout=timeout) as response:
    if urlparse(response.geturl()).hostname != SOURCE_HOST:
      raise ValueError("Geofabrik index redirected outside its HTTPS host")
    if response.status != 200:
      raise OSError(f"Geofabrik index returned HTTP {response.status}")
    data = response.read(MAX_INDEX_BYTES + 1)
    modified = response.headers.get("Last-Modified")
  if len(data) > MAX_INDEX_BYTES:
    raise ValueError("Geofabrik index exceeds size limit")
  return data, modified


def _epoch_from_http_date(value: str | None) -> float:
  if not value:
    return time.time()
  try:
    return parsedate_to_datetime(value).astimezone(timezone.utc).timestamp()
  except (TypeError, ValueError, OverflowError):
    return time.time()


def probe_source(source: MapSource, timeout: float = 20.) -> dict:
  request = Request(source.url, method="HEAD", headers={"User-Agent": "NAP-on-device-map-import/1"})
  try:
    response_context = urlopen(request, timeout=timeout)
  except HTTPError as error:
    if error.code not in (405, 501):
      raise
    # Some mirrors reject HEAD; a one-byte range provides the same validators
    # and total object length without downloading a country extract.
    response_context = urlopen(Request(source.url, headers={"User-Agent": "NAP-on-device-map-import/1",
                                                             "Range": "bytes=0-0",
                                                             "Accept-Encoding": "identity"}), timeout=timeout)
  with response_context as response:
    if urlparse(response.geturl()).hostname != SOURCE_HOST:
      raise ValueError("Geofabrik PBF redirected outside its HTTPS host")
    status = getattr(response, "status", 200)
    size = response.headers.get("Content-Length")
    size = int(size) if size and size.isdigit() else None
    if status == 206:
      match = re.fullmatch(r"bytes 0-0/(\d+)", response.headers.get("Content-Range", ""))
      if not match:
        raise ValueError("invalid one-byte Geofabrik size probe")
      size = int(match.group(1))
    if size is not None and not 0 < size <= MAX_SOURCE_BYTES:
      raise ValueError("invalid Geofabrik PBF size")
    modified = response.headers.get("Last-Modified")
    return {"bytes": size, "last_modified": modified,
            "etag": response.headers.get("ETag"), "snapshot_epoch": _epoch_from_http_date(modified)}


def _valid_partial(path: Path) -> int:
  if not path.exists():
    return 0
  if path.is_symlink():
    raise ValueError("partial PBF path must not be a symlink")
  size = path.stat().st_size
  return size if 0 < size <= MAX_SOURCE_BYTES else 0


def _sha256_file(path: Path) -> str:
  digest = hashlib.sha256()
  with Path(path).open("rb") as source_file:
    for block in iter(lambda: source_file.read(1024 * 1024), b""):
      digest.update(block)
  return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict) -> None:
  temporary = path.with_name(path.name + ".tmp")
  if temporary.is_symlink():
    raise ValueError("refusing symlink as map metadata temporary file")
  temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
  os.replace(temporary, path)


def download_pbf(source: MapSource, directory: Path, timeout: float = 45., *, opener=urlopen,
                 should_cancel=None, expected_size: int | None = None,
                 expected_etag: str | None = None, expected_last_modified: str | None = None, progress=None) -> dict:
  """Resume one HTTPS PBF download, verify transport length, and atomically stage it."""
  directory = Path(directory)
  directory.mkdir(parents=True, exist_ok=True)
  if directory.is_symlink():
    raise ValueError("PBF directory must not be a symlink")
  partial = directory / f"{source.package_id}.osm.pbf.part"
  sidecar = directory / f"{source.package_id}.osm.pbf.part.json"
  final = directory / f"{source.package_id}.osm.pbf"
  final_manifest = directory / f"{source.package_id}.osm.pbf.json"
  if final.is_symlink() or final_manifest.is_symlink() or sidecar.is_symlink():
    raise ValueError("staged PBF paths must not be symlinks")
  if final.exists() and final_manifest.exists():
    try:
      ready = json.loads(final_manifest.read_text(encoding="utf-8"))
      same_source = (ready.get("url") == source.url and final.stat().st_size == ready.get("bytes")
                     and (expected_size is None or ready.get("bytes") == expected_size)
                     and (not expected_etag or expected_etag == ready.get("etag"))
                     and (not expected_last_modified or expected_last_modified == ready.get("last_modified")))
      if same_source and _sha256_file(final) == ready.get("sha256"):
        return {"path": str(final), **ready}
    except (OSError, ValueError, TypeError):
      pass
    final.unlink(missing_ok=True)
    final_manifest.unlink(missing_ok=True)
  elif final.exists() or final_manifest.exists():
    final.unlink(missing_ok=True)
    final_manifest.unlink(missing_ok=True)
  offset = _valid_partial(partial)
  headers = {"User-Agent": "NAP-on-device-map-import/1", "Accept-Encoding": "identity"}
  if offset:
    try:
      previous = json.loads(sidecar.read_text())
      if previous.get("url") != source.url:
        raise ValueError("partial belongs to a different source")
      if expected_size is not None and previous.get("bytes_total") not in (None, expected_size):
        raise ValueError("partial belongs to a different source size")
      if expected_etag and previous.get("etag") and expected_etag != previous.get("etag"):
        raise ValueError("partial belongs to a different source version")
      if expected_last_modified and previous.get("last_modified") and expected_last_modified != previous.get("last_modified"):
        raise ValueError("partial belongs to a different source version")
      if previous.get("etag", "").startswith('"'):
        headers["If-Range"] = previous["etag"]
      elif previous.get("last_modified"):
        headers["If-Range"] = previous["last_modified"]
      else:
        raise ValueError("partial has no safe HTTP validator")
    except (OSError, ValueError, AttributeError):
      partial.unlink(missing_ok=True)
      sidecar.unlink(missing_ok=True)
      offset = 0
  if offset:
    try:
      partial_meta = json.loads(sidecar.read_text(encoding="utf-8"))
      total_saved = partial_meta.get("bytes_total")
      if total_saved is not None and offset == total_saved:
        digest_value = _sha256_file(partial)
        complete = {"url": source.url, "bytes": offset, "sha256": digest_value,
                    "last_modified": partial_meta.get("last_modified"), "etag": partial_meta.get("etag"),
                    "snapshot_epoch": _epoch_from_http_date(partial_meta.get("last_modified"))}
        os.replace(partial, final)
        _write_json_atomic(final_manifest, complete)
        sidecar.unlink(missing_ok=True)
        return {"path": str(final), **complete}
      if total_saved is not None and offset > total_saved:
        partial.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        offset = 0
    except (OSError, ValueError, TypeError):
      pass
    if offset:
      headers["Range"] = f"bytes={offset}-"
  request = Request(source.url, headers=headers)

  with opener(request, timeout=timeout) as response:
    if urlparse(response.geturl()).hostname != SOURCE_HOST:
      raise ValueError("Geofabrik PBF redirected outside its HTTPS host")
    status = getattr(response, "status", 200)
    if status not in (200, 206):
      raise OSError(f"Geofabrik PBF returned HTTP {status}")
    if offset and status == 206:
      content_range = response.headers.get("Content-Range", "")
      match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
      if not match or int(match.group(1)) != offset:
        raise ValueError("invalid resumed PBF Content-Range")
      total = int(match.group(3)) if match.group(3) != "*" else None
      response_end = int(match.group(2))
      response_length = response_end - offset + 1
      content_length = response.headers.get("Content-Length")
      if response_end < offset or (content_length and content_length.isdigit() and int(content_length) != response_length):
        raise ValueError("invalid resumed PBF range length")
      previous_etag = json.loads(sidecar.read_text()).get("etag")
      previous_modified = json.loads(sidecar.read_text()).get("last_modified")
      response_etag = response.headers.get("ETag")
      response_modified = response.headers.get("Last-Modified")
      if (previous_etag and response_etag and previous_etag != response_etag) or (
          not previous_etag and previous_modified and response_modified and previous_modified != response_modified):
        raise ValueError("PBF source changed during resumed download")
    else:
      if status == 206:
        raise ValueError("server returned partial content without a resume request")
      offset = 0
      total_header = response.headers.get("Content-Length")
      total = int(total_header) if total_header and total_header.isdigit() else None
    if total is not None and not 0 < total <= MAX_SOURCE_BYTES:
      raise ValueError("Geofabrik PBF exceeds the per-country limit")
    if expected_size is not None and total is not None and total != expected_size:
      if offset and status == 206:
        raise ValueError("PBF source size changed during resumed download")
    if expected_size is not None:
      total = expected_size

    mode = "ab" if offset and status == 206 else "wb"
    last_modified = response.headers.get("Last-Modified") or expected_last_modified
    etag = response.headers.get("ETag") or expected_etag
    if mode == "wb":
      sidecar.write_text(json.dumps({"url": source.url, "last_modified": last_modified, "etag": etag,
                                     "bytes_total": total}), encoding="utf-8")
    last_progress = 0.
    with partial.open(mode) as output:
      while block := response.read(1024 * 1024):
        if should_cancel and should_cancel():
          raise InterruptedError("map selection was removed during download")
        if output.tell() + len(block) > MAX_SOURCE_BYTES:
          raise ValueError("PBF download exceeds per-country limit")
        output.write(block)
        if progress and time.monotonic() - last_progress >= 1.:
          progress(output.tell(), total)
          last_progress = time.monotonic()
      output.flush()
      os.fsync(output.fileno())

  actual_size = partial.stat().st_size
  if total is not None and actual_size != total:
    raise OSError("PBF download is incomplete; the partial file is kept for resume")
  digest = _sha256_file(partial)
  os.replace(partial, final)
  sidecar.unlink(missing_ok=True)
  result = {"url": source.url, "bytes": actual_size, "sha256": digest,
            "last_modified": last_modified, "etag": etag,
            "snapshot_epoch": _epoch_from_http_date(last_modified)}
  _write_json_atomic(final_manifest, result)
  return {"path": str(final), **result}
