"""Radar cancellation and firmware-write protection, without hardware access."""
import ast
import importlib.util
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if sys.platform == "win32":
  # Exercise the actual runner methods on hosts without pyray/native Params.
  tree = ast.parse((ROOT / "run_script.py").read_text(encoding="utf-8"))
  namespace = {"signal": signal, "subprocess": subprocess,
               "rl": SimpleNamespace(Rectangle=lambda *args: args)}
  exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,(ast.Assign,ast.ClassDef))],type_ignores=[]),"run_script.py","exec"),namespace)
  Runner, State = namespace['ScriptRunnerApp'], namespace['ScriptState']
else:
  from scripts.nap.run_script import ScriptRunnerApp as Runner, ScriptState as State
  namespace = sys.modules[Runner.__module__].__dict__

class RadarRunnerExitTests(unittest.TestCase):
  def runner(self, module, state=None, process=None):
    obj = Runner.__new__(Runner)
    obj._script_module, obj._state = module, State.RUNNING if state is None else state
    obj._process = process if process is not None else Mock(poll=Mock(return_value=None))
    obj._params, obj._output_lines = Mock(), []
    obj._start_button, obj._exit_button = Mock(), Mock()
    return obj

  def button(self, obj):
    obj._render_buttons(SimpleNamespace(x=0,y=0,width=2160,height=1080))
    return obj._exit_button.set_enabled.call_args.args[0]

  def exit_with_mocks(self, obj):
    gui, hardware = Mock(), Mock()
    with patch.dict(namespace,{'gui_app':gui,'HARDWARE':hardware,'PC':False}):obj._on_exit_clicked()
    return gui, hardware

  def test_running_radar_calibration_and_test_buttons_are_enabled(self):
    for module in ('scripts.nap.calibrate_radar','scripts.nap.test_radar'):
      with self.subTest(module=module):self.assertTrue(self.button(self.runner(module)))

  def test_running_firmware_and_other_scripts_remain_blocked(self):
    for module in ('scripts.nap.flash_epas','scripts.nap.restore_epas','scripts.nap.calibrate_pedal','scripts.nap.test_radar_extra','unknown'):
      with self.subTest(module=module):
        obj=self.runner(module);self.assertFalse(self.button(obj))
        gui,hardware=self.exit_with_mocks(obj)
        obj._process.send_signal.assert_not_called();obj._params.put_bool.assert_not_called()
        gui.request_close.assert_not_called();hardware.reboot.assert_not_called()

  def test_reader_error_cannot_unlock_a_running_firmware_process(self):
    obj=self.runner('scripts.nap.flash_epas',State.ERROR)
    self.assertFalse(self.button(obj));self.exit_with_mocks(obj)
    obj._process.send_signal.assert_not_called()

  def test_ready_and_completed_firmware_can_exit(self):
    for state in (State.READY,State.COMPLETED,State.ERROR):
      obj=self.runner('scripts.nap.flash_epas',state,Mock(poll=Mock(return_value=0)))
      self.assertTrue(self.button(obj))

  def test_radar_exit_waits_for_sigint_cleanup_before_resuming_manager(self):
    for module in ('scripts.nap.calibrate_radar','scripts.nap.test_radar'):
      with self.subTest(module=module):
        obj=self.runner(module);events=[]
        obj._process.send_signal.side_effect=lambda value:events.append(('signal',value))
        obj._process.wait.side_effect=lambda **_:events.append(('exited',True)) or 0
        obj._params.put_bool.side_effect=lambda key,value:events.append((key,value))
        gui,hardware=self.exit_with_mocks(obj)
        self.assertEqual(events,[('signal',signal.SIGINT),('exited',True),('NAPScriptRunning',False)])
        obj._process.terminate.assert_not_called();obj._process.kill.assert_not_called()
        gui.request_close.assert_called_once();hardware.reboot.assert_called_once()

  def test_stuck_radar_child_keeps_manager_paused(self):
    obj=self.runner('scripts.nap.test_radar')
    obj._process.wait.side_effect=subprocess.TimeoutExpired('radar',5)
    gui,hardware=self.exit_with_mocks(obj)
    obj._process.terminate.assert_called_once();obj._process.kill.assert_called_once()
    obj._params.put_bool.assert_not_called();gui.request_close.assert_not_called();hardware.reboot.assert_not_called()
    self.assertEqual(obj._state,State.ERROR)

  def test_radar_timeout_escalation_waits_before_resuming_manager(self):
    obj=self.runner('scripts.nap.test_radar')
    obj._process.wait.side_effect=[subprocess.TimeoutExpired('radar',5),0]
    self.exit_with_mocks(obj)
    obj._process.terminate.assert_called_once();obj._process.kill.assert_not_called()
    obj._params.put_bool.assert_called_once_with('NAPScriptRunning',False)

  def test_both_radar_tools_handle_interrupt_and_close_panda(self):
    for name in ('calibrate_radar','test_radar'):
      with self.subTest(module=name):
        spec=importlib.util.spec_from_file_location('tested_'+name,ROOT/(name+'.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        panda=Mock();panda.can_recv.side_effect=KeyboardInterrupt
        with patch.object(module,'connect_panda',return_value=panda),patch.object(module,'p'):
          self.assertEqual(module.main(),0)
        self.assertEqual(panda.set_safety_mode.call_args_list[-1].args,(0,))
        panda.close.assert_called_once()

if __name__ == '__main__':unittest.main()
