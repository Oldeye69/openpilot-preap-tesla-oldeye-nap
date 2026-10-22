import pytest
from openpilot.selfdrive.road_context.ocr_vision import agreement


@pytest.mark.parametrize('value', range(20, 131, 10))
def test_all_numeric_labels(value):
  assert agreement(str(value), str(value), 96) == value


@pytest.mark.parametrize('first,second,confidence', [('30', '50', 96), ('5O', '50', 96), ('50', '50', 80),
                                                   ('999', '999', 99), ('', '', 99), ('50', '50', float('nan'))])
def test_disagreement_and_unknown_rejected(first, second, confidence):
  assert agreement(first, second, confidence) is None
