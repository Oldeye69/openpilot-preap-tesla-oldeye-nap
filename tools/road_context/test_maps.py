"""Run map selector/downloader regressions without loading vehicle libraries."""
from pathlib import Path
import sys,types,uuid
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root/'.tools/pytest-runtime'))
package=types.ModuleType('openpilot');package.__path__=[str(root)];sys.modules['openpilot']=package
import pytest
base=root/'.test-tmp'/('maps-'+uuid.uuid4().hex)
assert base.resolve().is_relative_to(root.resolve())
base.parent.mkdir(exist_ok=True)
folder=root/'selfdrive/road_context/tests'
names=['test_map_sources.py','test_maps_state.py','test_maps_ui.py']
raise SystemExit(pytest.main(['-c',str(root/'tools/road_context/pytest.ini'),'--confcutdir',str(folder),'-p','no:cacheprovider','--basetemp',str(base),*[str(folder/n) for n in names if (folder/n).exists()]]))
