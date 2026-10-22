"""UI-only translation of alert text; never changes event/status/control data."""
import re
from openpilot.system.ui.lib.multilang import tr

PATTERNS = (
 (r"Drive above (.+) to engage", "Drive above {value} to engage"),
 (r"Steer Assist Unavailable Below (.+)", "Steer Assist Unavailable Below {value}"),
 (r"Drive Above (.+)", "Drive Above {value}"),
 (r"([0-9]+(?:\.[0-9]+)?)% full", "{value}% full"),
 (r"([0-9]+(?:\.[0-9]+)?)% used", "{value}% used"),
 (r"([0-9]+(?:\.[0-9]+)?)% frames dropped", "{value}% frames dropped"),
 (r"([0-9]+) seconds? remaining\. Press again to save early\.", "{value} seconds remaining. Press again to save early."),
)


def translate_alert_text(text):
  translated = tr(text)
  if translated != text:
    return translated
  for pattern, template in PATTERNS:
    match = re.fullmatch(pattern, text)
    if match:
      translated = tr(template)
      return translated.format(value=match[1]) if translated != template else text
  match = re.fullmatch(r"(Calibration|Recalibration): ([0-9]+)%", text)
  if match:
    return tr("{state}: {value}%").format(state=tr(match[1]),value=match[2])
  if text.startswith('Driving Personality: '):
    return tr('Driving Personality: {value}').format(value=tr(text.removeprefix('Driving Personality: ')))
  return text
