"""Speed policy accepts SpeedLimitContext ONLY. Enforcement cannot be a control input."""
from dataclasses import dataclass
from openpilot.selfdrive.road_context.types import SpeedLimitContext, finite


@dataclass(frozen=True)
class PolicySettings:
  mode: str = 'DISPLAY_ONLY'
  experimental_enabled: bool = False
  offset_mps: float = 0.0
  allow_increase: bool = False


@dataclass(frozen=True)
class PolicyDecision:
  proposal: float | None = None
  target: float | None = None
  reason: str = 'DISPLAY_ONLY'


class SpeedLimitPolicy:
  def __init__(self):
    self.overridden = False
    self.last_cap = None

  def update(self, context, settings, driver_target, *, accelerator=False, brake=False, manual_change=False,
             engaged=False, capability=False, commissioned=False, accepted=False, resume=False):
    if not isinstance(context, SpeedLimitContext):
      raise TypeError('Only speed context is accepted')
    if not engaged or resume:
      self.overridden = False
      self.last_cap = None
    if accelerator or brake or manual_change:
      self.overridden = True
      self.last_cap = None
    if not finite(driver_target, settings.offset_mps) or not 0 <= settings.offset_mps <= 10:
      return PolicyDecision(reason='INVALID_SETTINGS')
    if (not context.valid or context.conflict or not context.control_eligible
        or not finite(context.effective_limit, context.confidence, context.age, context.road_match_confidence)
        or not .85 <= context.confidence <= 1 or not 0 <= context.age <= 2
        or not .85 <= context.road_match_confidence <= 1 or not context.direction_match
        or context.source not in ('MAP', 'CAMERA', 'NAVIGATION', 'DYNAMIC_MAP')
        or context.unit != 'm/s' or not 0 < context.effective_limit <= 70):
      return PolicyDecision(reason='UNQUALIFIED_LIMIT')
    proposal = context.effective_limit + settings.offset_mps
    if settings.mode == 'DISPLAY_ONLY':
      return PolicyDecision(proposal=proposal)
    if settings.mode not in ('ASSIST', 'AUTO'):
      return PolicyDecision(reason='INVALID_MODE')
    if self.overridden:
      return PolicyDecision(proposal=proposal, reason='DRIVER_OVERRIDE')
    if not engaged or not capability or not commissioned:
      return PolicyDecision(proposal=proposal, reason='VEHICLE_UNAVAILABLE')
    if settings.mode == 'ASSIST' and not accepted:
      return PolicyDecision(proposal=proposal, reason='ACCEPTANCE_REQUIRED')
    if settings.mode == 'AUTO' and not settings.experimental_enabled:
      return PolicyDecision(proposal=proposal, reason='AUTO_DISABLED')
    cap = proposal if settings.allow_increase else min(proposal, driver_target, self.last_cap if self.last_cap is not None else driver_target)
    self.last_cap = cap
    return PolicyDecision(proposal, cap, 'ACCEPTED' if settings.mode == 'ASSIST' else 'AUTO')


def smooth_cap(previous, target, dt, reduction_rate):
  """Caller supplies existing planner comfort bound; no PRE_AP-specific deceleration here."""
  if not finite(previous, target, dt, reduction_rate) or not 0 < dt <= 1 or reduction_rate <= 0:
    return previous
  return max(target, previous - reduction_rate * dt) if target < previous else target
