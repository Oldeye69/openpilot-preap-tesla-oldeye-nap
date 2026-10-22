"""Red circular sign proposals plus offline LSTM digit recognition.

OCR confidence is not road applicability. Observations remain advisory and
unqualified for vehicle control until a lane-relevance validation exists.
"""
import hashlib
import importlib.util
import sys
from pathlib import Path
import numpy as np
from openpilot.selfdrive.road_context.tsr import NUMERIC_LIMITS, SignObservation
from openpilot.selfdrive.road_context.vision import PrototypeVision

MODEL_SHA256 = '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'
BEST_MODEL_SHA256 = '8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba'


def agreement(first, second, confidence):
  if (first != second or not first.isascii() or not first.isdigit()
      or int(first) not in NUMERIC_LIMITS or not 90 <= confidence <= 100):
    return None
  return int(first)


class OCRVision:
  def __init__(self, model):
    # AGNOS venv is read-only. The deployment installer owns this isolated,
    # pinned optional dependency directory; map/API data never supplies paths.
    if (importlib.util.find_spec('cv2') is None or importlib.util.find_spec('tesserocr') is None) and Path('/data/road-context/runtime').is_dir():
      sys.path.append('/data/road-context/runtime')
    import cv2
    with Path(model).open('rb') as stream:
      if hashlib.file_digest(stream, 'sha256').hexdigest() not in (MODEL_SHA256, BEST_MODEL_SHA256):
        raise ValueError('Unrecognized OCR model')
    self.cv = cv2
    cv2.setNumThreads(1)
    import tesserocr
    self.ocr = tesserocr.PyTessBaseAPI(path=str(Path(model).parent), lang='eng', psm=tesserocr.PSM.SINGLE_LINE)
    self.ocr.SetVariable('tessedit_char_whitelist', '0123456789')
    self.lighting = PrototypeVision()
    self.tracks = []

  def process(self, rgb, timestamp, frame_id):
    cv = self.cv
    _, lights = self.lighting.process(rgb[::4, ::4] if rgb is not None else None, timestamp, frame_id)
    if rgb is None:
      self.tracks = []
      return [], lights
    h, w = rgb.shape[:2]
    hsv = cv.cvtColor(rgb, cv.COLOR_RGB2HSV)
    mask = (((hsv[:, :, 0] < 12) | (hsv[:, :, 0] > 168)) & (hsv[:, :, 1] > 95) & (hsv[:, :, 2] > 65)).astype(np.uint8)*255
    mask = cv.morphologyEx(mask, cv.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv.findContours(mask, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
    proposals = []
    for contour in contours:
      x, y, width, height = cv.boundingRect(contour)
      perimeter = cv.arcLength(contour, True)
      area = cv.contourArea(contour)
      if (not 20 <= width <= min(300, w*.35) or not .8 <= width/height <= 1.2
          or perimeter <= 0 or 4*np.pi*area/perimeter**2 < .72 or y+height > h*.85):
        continue
      proposals.append((area, x, y, width, height))
    signs, tracks = [], []
    # Bound CPU work independently of the number of red objects in a frame.
    for _, x, y, width, height in sorted(proposals, reverse=True)[:2]:
      crop = rgb[y+int(height*.23):y+int(height*.77), x+int(width*.15):x+int(width*.85)]
      gray = cv.cvtColor(crop, cv.COLOR_RGB2GRAY)
      # The red ring can enter the rectangular crop at its corners. It is not
      # glyph ink; retaining it distorts OCR scale and segmentation.
      crop_hsv = cv.cvtColor(crop, cv.COLOR_RGB2HSV)
      gray[crop_hsv[:, :, 1] > 95] = 255
      if gray.size == 0 or np.percentile(gray, 98)-np.percentile(gray, 2) < 55:
        continue
      _, ink = cv.threshold(gray, 0, 255, cv.THRESH_BINARY_INV | cv.THRESH_OTSU)
      points = cv.findNonZero(ink)
      if points is None:
        continue
      bx, by, bw, bh = cv.boundingRect(points)
      if bh < 8 or bw < 8:
        continue
      gray = gray[by:by+bh, bx:bx+bw]
      scaled_width = max(1, round(bw*48/bh))
      if scaled_width > 180:
        continue
      letters = cv.resize(gray, (scaled_width, 48), interpolation=cv.INTER_CUBIC)
      expanded = cv.copyMakeBorder(letters, 12, 12, 12, 12, cv.BORDER_CONSTANT, value=255)
      self.ocr.SetPageSegMode(7)
      self.ocr.SetImageBytes(expanded.tobytes(), expanded.shape[1], expanded.shape[0], 1, expanded.shape[1])
      text, confidence = self.ocr.GetUTF8Text().strip(), self.ocr.MeanTextConf()
      self.ocr.SetPageSegMode(8)
      self.ocr.SetImageBytes(expanded.tobytes(), expanded.shape[1], expanded.shape[0], 1, expanded.shape[1])
      second = self.ocr.GetUTF8Text().strip()
      if agreement(text, second, confidence) is None:
        continue
      confidence /= 100.
      value, box = int(text), (x/w, y/h, (x+width)/w, (y+height)/h)
      count = max((n+1 for v, b, t, n in self.tracks if v == value and 0 < timestamp-t <= 1.1
                   and abs(b[0]-box[0])+abs(b[1]-box[1]) < .12), default=1)
      tracks.append((value, box, timestamp, count))
      if count >= 3:
        signs.append(SignObservation('LIMIT', value, confidence, timestamp, frame_id, box,
                                     source='OCR_CONSENSUS_RED_CIRCLE_UNVALIDATED', qualified=False))
    self.tracks = tracks
    return signs, lights
