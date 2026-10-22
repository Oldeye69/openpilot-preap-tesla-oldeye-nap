"""Run portable association regressions without native vehicle libraries."""
from pathlib import Path
import sys
import types

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root/'.tools/pytest-runtime'))
package = types.ModuleType('openpilot')
package.__path__ = [str(root)]
sys.modules['openpilot'] = package
import pytest
names = ['test_road_association.py', 'test_association_pipeline.py', 'test_tsr_ui_regression.py', 'test_display.py', 'test_ocr.py']
folder = root/'selfdrive/road_context/tests'
raise SystemExit(pytest.main(['-c', str(root/'tools/road_context/pytest.ini'), '--confcutdir', str(folder),
                             '-p', 'no:cacheprovider', *[str(folder/name) for name in names]]))
