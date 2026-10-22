"""Offline OSM prototype: local versioned SQLite, geometric/directional matching, no API calls."""
import hashlib
from contextlib import closing
import json
import math
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from openpilot.selfdrive.road_context.types import LimitObservation, RoadMatch

EARTH = 6371000.0
RULES = json.loads((Path(__file__).parent / 'data/country_rules.json').read_text())


def xy(point, origin):
  return (math.radians(point[1] - origin[1]) * EARTH * math.cos(math.radians(origin[0])),
          math.radians(point[0] - origin[0]) * EARTH)


def distance(a, b):
  return math.hypot(*xy(a, b))


def angle(a, b):
  return abs((a - b + 180) % 360 - 180)


def project(point, geometry):
  best, along, total = None, 0.0, 0.0
  for a, b in zip(geometry[:-1], geometry[1:], strict=True):
    ax, ay = xy(a, point)
    bx, by = xy(b, point)
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length < .01:
      continue
    t = min(1., max(0., -(ax * dx + ay * dy) / (length * length)))
    d = math.hypot(ax + t * dx, ay + t * dy)
    candidate = (d, total + t * length, math.degrees(math.atan2(dx, dy)) % 360)
    if best is None or d < best[0]:
      best, along = candidate, candidate[1]
    total += length
  return (best[0], along, total, best[2]) if best else (math.inf, 0, 0, 0)


def numeric_limit(text):
  m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(km/h|mph)?', text.strip())
  if not m:
    return None
  value = float(m[1]) * (.44704 if m[2] == 'mph' else 1 / 3.6)
  return value if 0 < value <= 70 else None


def conditional_limit(text, local_datetime=None, wet=None):
  """Explicit small grammar. Unhandled clauses are unknown, never silently ignored."""
  m = re.fullmatch(r'([^@;]+)\s*@\s*\(([^)]+)\)', text.strip())
  if not m or numeric_limit(m[1]) is None:
    return None, 'UNRESOLVED_CONDITION'
  condition = m[2].strip()
  if condition == 'wet':
    return (numeric_limit(m[1]) if wet else None, 'ACTIVE' if wet else 'INACTIVE') if wet is not None else (None, 'UNRESOLVED_CONDITION')
  time_match = re.fullmatch(r'(\d{2}):(\d{2})-(\d{2}):(\d{2})', condition)
  if time_match and local_datetime is not None and local_datetime.utcoffset() is not None:
    a, b, c, d = map(int, time_match.groups())
    if a < 24 and c < 24 and b < 60 and d < 60:
      start, end, now = 60*a+b, 60*c+d, local_datetime.hour*60+local_datetime.minute
      active = start <= now < end if start <= end else now >= start or now < end
      return (numeric_limit(m[1]) if active else None), 'ACTIVE' if active else 'INACTIVE'
  return None, 'UNRESOLVED_CONDITION'


def resolve_limit(tags, country, forward=True, local_datetime=None, wet=None):
  suffix = 'forward' if forward else 'backward'
  condition = tags.get(f'maxspeed:{suffix}:conditional', tags.get('maxspeed:conditional'))
  if condition:
    value, state = conditional_limit(condition, local_datetime, wet)
    if state != 'INACTIVE':
      return value, 'CONDITIONAL', state
  if tags.get('maxspeed:variable') not in (None, 'no'):
    return None, 'VARIABLE', 'DYNAMIC_VALUE_UNAVAILABLE'
  text = tags.get('maxspeed:' + suffix, tags.get('maxspeed', ''))
  if text:
    numeric = numeric_limit(text)
    if numeric is not None:
      return numeric, 'POSTED', ''
    if text == 'none':
      return None, 'UNLIMITED', 'NO_NUMERIC_LIMIT'
    if ':' not in text:
      return None, 'UNKNOWN', 'UNSUPPORTED_TAG'
  implicit = text or tags.get('maxspeed:type', tags.get('source:maxspeed', ''))
  zone = ''
  if implicit.startswith(country + ':') and country:
    zone = implicit.split(':', 1)[1]
  elif tags.get('highway') == 'motorway':
    zone = 'motorway'
  elif tags.get('motorroad') == 'yes' and tags.get('highway') == 'trunk':
    zone = 'expressway'
  # Residential class alone does not prove a signed urban area.
  rules = RULES['countries'].get(country, {})
  value = rules.get(zone)
  return (value / 3.6 if isinstance(value, (int, float)) else None), 'IMPLICIT', '' if value else 'COUNTRY_CONTEXT_UNKNOWN'


@dataclass(frozen=True)
class Road:
  road_id: str
  geometry: list
  tags: dict
  country: str


def import_osm(xml_path, database, *, country, snapshot_epoch, source_url):
  """Regional OSM XML import, offline. Country override requires an already country-clipped extract."""
  nodes, roads = {}, []
  for _, elem in ET.iterparse(xml_path, events=('end',)):
    if elem.tag == 'node':
      nodes[elem.attrib['id']] = [float(elem.attrib['lat']), float(elem.attrib['lon'])]
      elem.clear()
    elif elem.tag == 'way':
      tags = {t.attrib['k']: t.attrib['v'] for t in elem.findall('tag')}
      refs = [n.attrib['ref'] for n in elem.findall('nd')]
      if tags.get('highway') and all(n in nodes for n in refs) and len(refs) > 1:
        roads.append(Road(elem.attrib['id'], [nodes[n] for n in refs], tags, tags.get('addr:country', country).upper()))
      elem.clear()
  digest = hashlib.sha256(Path(xml_path).read_bytes()).hexdigest()
  write_database(database, roads, {'version': digest, 'snapshot_epoch': snapshot_epoch, 'source': source_url,
                                 'license': 'ODbL-1.0', 'attribution': 'OpenStreetMap contributors', 'schema': 1})


def write_database(path, roads, metadata):
  if Path(path).exists():
    raise FileExistsError('Install new version atomically; never overwrite active map')
  with closing(sqlite3.connect(path)) as db, db:
    db.executescript('CREATE TABLE metadata(data TEXT); CREATE TABLE roads(id INTEGER PRIMARY KEY, road_id TEXT, geometry TEXT, tags TEXT, country TEXT); ' +
                     'CREATE VIRTUAL TABLE bounds USING rtree(id,minlat,maxlat,minlon,maxlon);')
    db.execute('INSERT INTO metadata VALUES(?)', (json.dumps(metadata),))
    for n, road in enumerate(roads):
      db.execute('INSERT INTO roads VALUES(?,?,?,?,?)', (n, road.road_id, json.dumps(road.geometry), json.dumps(road.tags), road.country))
      lat, lon = zip(*road.geometry, strict=True)
      db.execute('INSERT INTO bounds VALUES(?,?,?,?,?)', (n, min(lat), max(lat), min(lon), max(lon)))


class OSMProvider:
  def __init__(self, path):
    self.db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    self.metadata = json.loads(self.db.execute('SELECT data FROM metadata').fetchone()[0])
    if self.metadata.get('schema') != 1 or not self.metadata.get('version'):
      raise ValueError('Unsupported/unversioned map')
    self.current = None

  def close(self):
    self.db.close()

  def decode_road(self, row):
    tags = json.loads(row[2])
    country = row[3]
    # A download region is not jurisdiction evidence: complete border ways and
    # the combined Ireland/Northern Ireland extract extend beyond one country.
    if self.metadata.get('country') and self.metadata.get('country_context') != 'BOUNDARY_VERIFIED':
      explicit = tags.get('addr:country', '').upper()
      country = explicit if re.fullmatch('[A-Z]{2}', explicit) else ''
    return Road(row[0], json.loads(row[1]), tags, country)

  def get(self, road_id):
    row = self.db.execute('SELECT road_id,geometry,tags,country FROM roads WHERE road_id=?', (road_id,)).fetchone()
    return self.decode_road(row) if row else None

  def match(self, position, now, route=()):
    if not position or not position.fresh(now):
      self.current = None
      return RoadMatch(reason='GPS_UNAVAILABLE')
    lat, lon = position.latitude, position.longitude
    delta_lon = .0015 / max(.1, math.cos(math.radians(lat)))
    rows = self.db.execute('SELECT road_id,geometry,tags,country FROM roads JOIN bounds USING(id) ' +
                           'WHERE minlat<? AND maxlat>? AND minlon<? AND maxlon>? LIMIT 256',
                           (lat+.0015, lat-.0015, lon+delta_lon, lon-delta_lon)).fetchall()
    candidates = []
    for row in rows:
      road = self.decode_road(row)
      d, along, length, heading = project((lat, lon), road.geometry)
      forward = angle(heading, position.heading) <= angle(heading+180, position.heading)
      error = angle(heading if forward else heading+180, position.heading)
      if d > 60 or error > 50 or road.tags.get('oneway') == ('-1' if forward else 'yes'):
        continue
      try:
        layer = int(road.tags.get('layer', '0'))
      except (TypeError, ValueError):
        continue
      if position.layer is not None and position.layer != layer:
        continue
      score = math.exp(-d/max(8, position.accuracy)) * math.exp(-error/45)
      if route and road.road_id not in route:
        score *= .35
      if self.current and self.current.road_id == road.road_id:
        score = min(1., score + .03)
      candidates.append((score, road, forward, d, along, length))
    if not candidates:
      self.current = None
      return RoadMatch(reason='NO_MATCH')
    candidates.sort(key=lambda x: x[0], reverse=True)
    score, road, forward, d, along, length = candidates[0]
    if len(candidates) > 1 and score - candidates[1][0] < .2:
      score *= .5  # ambiguity, including grade separation without independent layer evidence
    if position.speed < 1.5:
      score = min(score, .65)  # no reliable direction from stationary GNSS heading
    self.current = RoadMatch(road.road_id, score, True, forward, road.tags.get('highway', ''), road.country,
                             along if forward else length-along, length, d, 'MATCHED' if score >= .7 else 'AMBIGUOUS')
    return self.current

  def observation(self, match, now, epoch, *, local_datetime=None, wet=None):
    road = self.get(match.road_id)
    if road is None:
      return None
    value, kind, reason = resolve_limit(road.tags, road.country, match.forward, local_datetime, wet)
    return LimitObservation('MAP', value, now, .95, road.road_id, ttl=2, direction_match=match.direction_match,
                            match_confidence=match.confidence, limit_type=kind, dataset_age=epoch-self.metadata['snapshot_epoch'],
                            version=self.metadata['version'], reason=reason, control_eligible=False)

  def upcoming(self, match, now, epoch, route):
    if match.road_id not in route or match.confidence < .85:
      return None
    current = self.get(match.road_id)
    current_value = resolve_limit(current.tags, current.country, match.forward)[0]
    endpoint = current.geometry[-1] if match.forward else current.geometry[0]
    remaining = match.length - match.along
    for road_id in route[route.index(match.road_id)+1:]:
      road = self.get(road_id)
      if road is None:
        break
      forward = distance(endpoint, road.geometry[0]) <= distance(endpoint, road.geometry[-1])
      start = road.geometry[0] if forward else road.geometry[-1]
      if distance(endpoint, start) > 5 or road.tags.get('oneway') == ('-1' if forward else 'yes'):
        break
      next_match = RoadMatch(road_id, match.confidence, True, forward, country=road.country)
      obs = self.observation(next_match, now, epoch)
      if obs.value is not None and obs.value != current_value:
        return obs, remaining
      if obs.value is None:
        break
      remaining += sum(distance(a, b) for a, b in zip(road.geometry[:-1], road.geometry[1:], strict=True))
      endpoint = road.geometry[-1] if forward else road.geometry[0]
    return None


class RegionalOSMProvider:
  """Select among installed country databases; absent regions remain unknown.

  Each database retains its own snapshot age. Close matches from different
  roads across a border lower confidence just like parallel roads do.
  """
  def __init__(self, directory):
    self.providers = [OSMProvider(path) for path in sorted(Path(directory).glob('*.sqlite'))
                      if not path.name.endswith('.building.sqlite')]
    if not self.providers:
      raise ValueError('No installed road databases')
    self.selected = None

  def close(self):
    for provider in self.providers:
      provider.close()

  def match(self, position, now, route=()):
    from dataclasses import replace
    candidates = [(provider.match(position, now, route), provider) for provider in self.providers]
    candidates = [(match, provider) for match, provider in candidates if match.road_id]
    if not candidates:
      self.selected = None
      return RoadMatch(reason='NO_MATCH')
    candidates.sort(key=lambda item: item[0].confidence, reverse=True)
    best, self.selected = candidates[0]
    if any(other.road_id != best.road_id and best.confidence-other.confidence < .2 for other, _ in candidates[1:]):
      best = replace(best, confidence=best.confidence*.5, reason='AMBIGUOUS')
    return best

  def get(self, road_id):
    return self.selected.get(road_id) if self.selected else None

  def observation(self, match, now, epoch, **kwargs):
    return self.selected.observation(match, now, epoch, **kwargs) if self.selected else None

  def upcoming(self, match, now, epoch, route):
    return self.selected.upcoming(match, now, epoch, route) if self.selected else None
