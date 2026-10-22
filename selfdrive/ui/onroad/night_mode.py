"""Display-only double-tap toggle for the dim on-road view."""


class NightModeGesture:
  INTERVAL = 0.4
  RADIUS = 80

  def __init__(self):
    self.reset()

  def reset(self):
    self.enabled = False
    self.down = None
    self.pending = None

  def press(self, now, x, y):
    self.down = (now, x, y)

  def release(self, now, x, y):
    down, self.down = self.down, None
    if down is None or not 0 <= now - down[0] <= self.INTERVAL:
      return
    if (x - down[1]) ** 2 + (y - down[2]) ** 2 > self.RADIUS ** 2:
      return

    if self.pending is not None:
      t, px, py = self.pending
      if 0 <= now - t <= self.INTERVAL and (x - px) ** 2 + (y - py) ** 2 <= self.RADIUS ** 2:
        self.pending = None
        self.enabled = not self.enabled
        return

    self.pending = (now, x, y)

  def flush(self, now):
    """Return whether an expired single tap should invoke the normal click."""
    if self.pending is not None and now - self.pending[0] > self.INTERVAL:
      self.pending = None
      return not self.enabled
    return False
