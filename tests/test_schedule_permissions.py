import plistlib
import unittest
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
from qoder_schedule import Scheduler
from qoder_schedule import ScheduleError, BackgroundPermissionError
import checkin_cli as cli


class PermissionTests(unittest.TestCase):
    def test_launchd_permission_failure_is_detected_and_probe_unloaded(self):
        with tempfile.TemporaryDirectory() as tmp, patch('qoder_schedule.Path.home', return_value=Path(tmp)):
            s = Scheduler(Path(tmp) / 'config.json', platform='darwin')
            calls = []
            def command(args):
                calls.append(args)
                if args[1] == 'bootstrap':
                    definition = plistlib.loads(Path(args[-1]).read_bytes())
                    self.assertNotIn('StartCalendarInterval', definition)
                    Path(definition['StandardErrorPath']).write_text('PermissionError\n')
                return subprocess.CompletedProcess(args, 0, 'last exit code = 1', '')
            with patch.object(s, 'command', side_effect=command):
                with self.assertRaises(BackgroundPermissionError):
                    s.preflight([])
            self.assertEqual(calls[-1][1], 'bootout')
            self.assertFalse(list(s.startup_log.parent.glob('preflight-*')))

    def test_other_failure_does_not_suggest_full_disk_access(self):
        s = Scheduler(Path('/tmp/qoder-config.json'), platform='darwin')
        with patch.object(s, 'preflight', side_effect=ScheduleError('启动失败')), \
                patch('checkin_cli.sys.stdin.isatty', return_value=True), \
                patch('builtins.input') as prompt:
            with self.assertRaises(ScheduleError):
                cli.check_background_access(s, [])
        prompt.assert_not_called()

    def test_launchd_does_not_open_documents_before_python(self):
        s = Scheduler(Path('/tmp/qoder-config.json'), platform='darwin')
        definition = plistlib.loads(s.definition())
        self.assertEqual(definition['WorkingDirectory'], str(Path.home()))
        self.assertEqual(definition['StandardErrorPath'], str(
            Path.home() / 'Library/Logs/qoder-checkin' / (s.key + '.log')))

    def test_non_macos_preflight_is_noop(self):
        s = Scheduler(Path('/tmp/qoder-config.json'), platform='win32')
        self.assertTrue(hasattr(s, 'preflight'))
        with patch.object(s, 'command') as command:
            s.preflight([])
        command.assert_not_called()

    def test_permission_failure_noninteractive_does_not_open_settings(self):
        self.assertTrue(hasattr(cli, 'check_background_access'))
        s = Scheduler(Path('/tmp/qoder-config.json'), platform='darwin')
        with patch.object(s, 'preflight', side_effect=BackgroundPermissionError('拒绝')), \
                patch('checkin_cli.sys.stdin.isatty', return_value=False), \
                patch('checkin_cli.subprocess.run') as launch:
            with self.assertRaises(ScheduleError):
                cli.check_background_access(s, [])
        launch.assert_not_called()

    def test_permission_retry_after_user_confirmation(self):
        self.assertTrue(hasattr(cli, 'check_background_access'))
        s = Scheduler(Path('/tmp/qoder-config.json'), platform='darwin')
        with patch.object(s, 'preflight', side_effect=[BackgroundPermissionError('拒绝'), None]) as probe, \
                patch('checkin_cli.sys.stdin.isatty', return_value=True), \
                patch('builtins.input', return_value=''), \
                patch('checkin_cli.subprocess.run') as launch:
            cli.check_background_access(s, [])
        self.assertEqual(probe.call_count, 2)
        launch.assert_called_once()
