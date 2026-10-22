import json
import pytest


@pytest.fixture
def policy_file(tmp_path):
  data = {'reviewed': '2026-09-24', 'max_review_age_days': 3650, 'default': {'state': 'UNKNOWN', 'mode': 'OFF'},
          'countries': {'HU': {'state': 'ALLOWED', 'mode': 'PRECISE'}, 'DE': {'state': 'PROHIBITED', 'mode': 'OFF'},
                        'FR': {'state': 'RESTRICTED', 'mode': 'DANGER_ZONE_ONLY'}}}
  path = tmp_path/'policy.json'
  path.write_text(json.dumps(data))
  return path
