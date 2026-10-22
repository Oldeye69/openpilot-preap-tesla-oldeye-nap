"""Small geometry primitives shared by the receive-only association and replay."""
from dataclasses import dataclass
import math
import numpy as np

device_frame_from_view_frame = np.array([[0., 0., 1.], [1., 0., 0.], [0., 1., 0.]])


def rot_from_euler(rpy):
  roll, pitch, yaw = rpy
  sr, sp, sy = map(math.sin, (roll, pitch, yaw))
  cr, cp, cy = map(math.cos, (roll, pitch, yaw))
  return np.array([[cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr],
                   [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr],
                   [-sp, cp*sr, cp*cr]])


@dataclass(frozen=True)
class CameraGeometry:
  """Explicit recorded/test camera intrinsics. Production supplies DEVICE_CAMERAS."""
  width: int
  height: int
  focal_length: float

  @property
  def size(self):
    return self.width, self.height

  @property
  def intrinsics_inv(self):
    return np.linalg.inv(np.array([[self.focal_length, 0., self.width/2],
                                   [0., self.focal_length, self.height/2], [0., 0., 1.]]))
