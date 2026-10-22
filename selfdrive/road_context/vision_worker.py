"""One bounded perception job; the message loop never waits for OCR."""
from concurrent.futures import ThreadPoolExecutor


class VisionWorker:
  def __init__(self, vision):
    self.vision = vision
    self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='tsr')
    self.pending = None
    self.stamp = -1.

  def submit(self, rgb, stamp, frame_id):
    if self.pending is not None:
      return False
    self.stamp = stamp
    self.pending = self.executor.submit(self.vision.process, rgb, stamp, frame_id)
    return True

  def poll(self, now):
    if self.pending is None or not self.pending.done():
      return None
    pending, self.pending = self.pending, None
    signs, lights = pending.result()
    if not 0 <= now-self.stamp <= .5:
      return [], {'camera_available': False}, self.stamp
    return signs, lights, self.stamp

  def close(self):
    self.executor.shutdown(wait=False, cancel_futures=True)
