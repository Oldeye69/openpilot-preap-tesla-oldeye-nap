"""Bounded CPU prototype, not a trained or road-qualified detector. No primary-model changes."""
from collections import deque
import numpy as np
from openpilot.selfdrive.road_context.tsr import NUMERIC_LIMITS, SignObservation

DIGITS = ('111101101101111', '010110010010111', '111001111100111', '111001111001111', '101101111001001',
          '111100111001111', '111100111101111', '111001001001001', '111101111101111', '111101111001111')


def glyph(value):
  digits = [np.array([int(c) for c in DIGITS[int(d)]], dtype=bool).reshape(5, 3) for d in str(value)]
  out = np.zeros((5, len(digits)*4-1), dtype=bool)
  for n, digit in enumerate(digits):
    out[:, n*4:n*4+3] = digit
  return out


def resize(mask, shape):
  y = np.minimum(((np.arange(shape[0])+.5)*mask.shape[0]/shape[0]).astype(int), mask.shape[0]-1)
  x = np.minimum(((np.arange(shape[1])+.5)*mask.shape[1]/shape[1]).astype(int), mask.shape[1]-1)
  return mask[y[:, None], x[None, :]]


def components(mask, max_pixels=60000):
  if int(mask.sum()) > max_pixels:
    return []
  seen = np.zeros_like(mask, dtype=bool)
  result = []
  h, w = mask.shape
  for y, x in zip(*np.where(mask), strict=True):
    if seen[y, x]:
      continue
    q = deque([(int(y), int(x))])
    seen[y, x] = True
    xmin = xmax = int(x)
    ymin = ymax = int(y)
    count = 0
    while q:
      py, px = q.popleft()
      count += 1
      xmin, xmax, ymin, ymax = min(xmin, px), max(xmax, px), min(ymin, py), max(ymax, py)
      for ny, nx in ((py-1, px), (py+1, px), (py, px-1), (py, px+1)):
        if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
          seen[ny, nx] = True
          q.append((ny, nx))
    if count >= 4:
      result.append((xmin, ymin, xmax+1, ymax+1, count))
  return result


class PrototypeVision:
  def process(self, rgb, timestamp, frame_id):
    if rgb is None or rgb.ndim != 3 or rgb.shape[2] != 3:
      return [], {'camera_available': False}
    image = rgb[::max(1, rgb.shape[0]//240), ::max(1, rgb.shape[1]//400)]
    r, g, b = (image[:, :, n].astype(float) for n in range(3))
    h, w = r.shape
    red = (r > 100) & (r > g*1.5) & (r > b*1.5)
    signs = []
    for x1, y1, x2, y2, count in components(red):
      width, height = x2-x1, y2-y1
      if not 12 <= width <= 140 or not .75 <= width/height <= 1.25 or not .08 < count/(width*height) < .65:
        continue
      crop = image[y1+height//5:y2-height//5, x1+width//5:x2-width//5].max(axis=2)
      black = crop < 90
      ys, xs = np.where(black)
      if len(xs) < 6:
        continue
      black = black[ys.min():ys.max()+1, xs.min():xs.max()+1]
      scores = [(float(np.mean(resize(black, glyph(value).shape) == glyph(value))), value) for value in NUMERIC_LIMITS]
      scores.sort(reverse=True)
      if scores[0][0] < .8 or scores[0][0]-scores[1][0] < .015:
        continue
      signs.append(SignObservation('LIMIT', scores[0][1], min(.69, scores[0][0]), timestamp, frame_id,
                                   (x1/w, y1/h, x2/w, y2/h)))
    gray = image.mean(axis=2)
    bright = (gray > 225) & (np.max(image, axis=2)-np.min(image, axis=2) < 45)
    bright_boxes = components(bright)
    red_boxes = components(red)
    pairs = any(abs((a[1]+a[3])-(c[1]+c[3])) < 8 and 5 < abs(a[0]-c[0]) < w*.3
                for i, a in enumerate(bright_boxes) for c in bright_boxes[i+1:])
    lead_hint = any(y1 > h*.35 and y2 < h*.9 and x1 > w*.15 and x2 < w*.85 for x1, y1, x2, y2, _ in red_boxes)
    urban_hint = sum(y2 < h*.45 for _, _, _, y2, _ in bright_boxes) >= 3
    # Absence of bright blobs cannot establish absence of vehicles. Never export confident clearance.
    return signs, {'camera_available': True, 'ambient': 'DARK' if float(np.median(gray)) < 45 else 'BRIGHT',
                   'oncoming': True if pairs else None, 'lead': True if lead_hint else None,
                   'street_lighting': True if urban_hint else None, 'confidence': .5}


def nv12_rgb(data, width, height, stride, uv_offset, step=4):
  raw = np.frombuffer(data, dtype=np.uint8)
  if width <= 0 or height <= 0 or stride < width or len(raw) < uv_offset + stride*(height//2):
    raise ValueError('Invalid NV12 frame')
  # Sample before conversion; no full-resolution RGB copy on 3X.
  yy = raw[:stride*height].reshape(height, stride)[::step, :width:step].astype(float)
  uv = raw[uv_offset:uv_offset+stride*(height//2)].reshape(height//2, stride)
  rows = np.arange(0, height, step)//2
  cols = (np.arange(0, width, step)//2)*2
  u, v = uv[rows[:, None], cols[None, :]].astype(float)-128, uv[rows[:, None], cols[None, :]+1].astype(float)-128
  return np.clip(np.stack((yy+1.402*v, yy-.344*u-.714*v, yy+1.772*u), axis=2), 0, 255).astype(np.uint8)
