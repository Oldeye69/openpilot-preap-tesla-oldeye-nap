"""Verified, resumable installation of prebuilt regional OSM SQLite maps.

The device downloads only preprocessed databases. Raw OSM PBF/XML imports are
deliberately kept off-device. Downloads are staged, size/hash/schema checked,
and atomically renamed so an interrupted transfer never replaces a usable map.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_MAP_BYTES = 8 * 1024 * 1024 * 1024
ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
SHA_RE = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class MapPackage:
  package_id: str
  name: str
  url: str
  size: int
  sha256: str
  countries: tuple[str, ...]
  version: str


def _https_url(value: str, base_url: str) -> str:
  resolved = urljoin(base_url, value)
  parsed, base = urlparse(resolved), urlparse(base_url)
  if parsed.scheme != "https" or parsed.hostname != base.hostname or parsed.port != base.port:
    raise ValueError("map URL must use the configured HTTPS catalog host")
  return resolved


def parse_manifest(document: bytes, catalog_url: str) -> list[MapPackage]:
  if len(document) > MAX_MANIFEST_BYTES:
    raise ValueError("map catalog exceeds size limit")
  payload = json.loads(document)
  if payload.get("schema_version") != SCHEMA_VERSION or payload.get("coverage") != "Europe" or payload.get("license") != "ODbL-1.0":
    raise ValueError("unsupported catalog schema or missing ODbL metadata")
  if payload.get("attribution") != "OpenStreetMap contributors":
    raise ValueError("missing OpenStreetMap attribution")
  packages = []
  seen = set()
  for item in payload.get("packages", []):
    package_id = item.get("id", "")
    countries = tuple(item.get("countries", []))
    size, digest, version = item.get("bytes"), item.get("sha256", ""), item.get("version", "")
    if not ID_RE.fullmatch(package_id) or package_id in seen:
      raise ValueError("invalid or duplicate package id")
    if not isinstance(size, int) or not 0 < size <= MAX_MAP_BYTES:
      raise ValueError(f"invalid map size for {package_id}")
    if not SHA_RE.fullmatch(digest) or not version or not countries or any(not re.fullmatch(r"[A-Z]{2}", c) for c in countries):
      raise ValueError(f"invalid map metadata for {package_id}")
    packages.append(MapPackage(package_id, str(item["name"]), _https_url(item["url"], catalog_url),
                               size, digest, countries, version))
    seen.add(package_id)
  if not packages:
    raise ValueError("catalog contains no map packages")
  return packages


def fetch_manifest(catalog_url: str, timeout: float = 15.) -> bytes:
  if urlparse(catalog_url).scheme != "https":
    raise ValueError("map catalog must use HTTPS")
  with urlopen(Request(catalog_url, headers={"User-Agent": "NAP-map-manager/1"}), timeout=timeout) as response:
    if urlparse(response.geturl()).hostname != urlparse(catalog_url).hostname:
      raise ValueError("catalog server redirected outside its HTTPS host")
    if response.status != 200:
      raise OSError(f"catalog returned HTTP {response.status}")
    data = response.read(MAX_MANIFEST_BYTES + 1)
  if len(data) > MAX_MANIFEST_BYTES:
    raise ValueError("map catalog exceeds size limit")
  return data


def _valid_partial(path: Path, package: MapPackage) -> int:
  if not path.exists():
    return 0
  if path.is_symlink():
    raise ValueError("partial map path must not be a symlink")
  size = path.stat().st_size
  # A Range continuation is safe only for a prefix of the advertised object.
  return size if 0 < size < package.size else 0


def download_package(package: MapPackage, destination: Path, *, timeout: float = 30., opener=urlopen) -> dict:
  """Download, verify and atomically install one package. Raises on any failure."""
  destination = Path(destination)
  destination.mkdir(parents=True, exist_ok=True)
  if destination.is_symlink():
    raise ValueError("map destination must not be a symlink")
  final_path = destination / f"{package.package_id}.sqlite"
  partial_path = destination / f".{package.package_id}.sqlite.part"
  offset = _valid_partial(partial_path, package)
  digest = hashlib.sha256()
  if offset:
    with partial_path.open("rb") as existing:
      for block in iter(lambda: existing.read(1024 * 1024), b""):
        digest.update(block)
  headers = {"User-Agent": "NAP-map-manager/1", "Accept-Encoding": "identity"}
  if offset:
    headers["Range"] = f"bytes={offset}-"
  request = Request(package.url, headers=headers)
  with opener(request, timeout=timeout) as response:
    if urlparse(response.geturl()).hostname != urlparse(package.url).hostname:
      raise ValueError("map server redirected outside the catalog host")
    status = getattr(response, "status", 200)
    if offset and status != 206:
      offset, digest = 0, hashlib.sha256()
    mode = "ab" if offset else "wb"
    if status not in (200, 206):
      raise OSError(f"map server returned HTTP {status}")
    with partial_path.open(mode) as output:
      while block := response.read(1024 * 1024):
        if output.tell() + len(block) > package.size:
          raise ValueError("map download exceeds advertised size")
        output.write(block)
        digest.update(block)
      output.flush()
      os.fsync(output.fileno())
  actual_size = partial_path.stat().st_size
  if actual_size != package.size or digest.hexdigest() != package.sha256:
    raise ValueError("map size or SHA-256 verification failed")
  validate_database(partial_path, package)
  os.replace(partial_path, final_path)
  return {"id": package.package_id, "path": str(final_path), "bytes": actual_size,
          "sha256": package.sha256, "verified_at": time.time()}


def validate_database(path: Path, package: MapPackage) -> None:
  db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
  try:
    integrity = db.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
      raise ValueError("SQLite integrity_check failed")
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"metadata", "roads"}.issubset(tables):
      raise ValueError("map database has an unsupported schema")
    metadata_row = db.execute("SELECT data FROM metadata LIMIT 1").fetchone()
    if not metadata_row:
      raise ValueError("map database has no metadata")
    metadata = json.loads(metadata_row[0])
    if metadata.get("schema") != SCHEMA_VERSION or metadata.get("version") != package.version:
      raise ValueError("map database version does not match catalog")
    count = db.execute("SELECT COUNT(*) FROM roads").fetchone()[0]
    countries = {r[0] for r in db.execute("SELECT DISTINCT country FROM roads WHERE country != ''")}
    if count <= 0 or not countries.issubset(set(package.countries)) or not countries:
      raise ValueError("map database is empty or contains unexpected countries")
  finally:
    db.close()
