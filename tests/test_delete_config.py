import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import checkin_cli as cli
from qoder_schedule import Scheduler, ScheduleError


class DeleteConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / 'custom.json'
        self.config.write_text('invalid json is also removable')

    def run_delete(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                return cli.main(['delete-config', '--config', str(self.config)])
            except SystemExit as exc:
                return exc.code

    def test_only_config_removed_and_repeat_is_safe(self):
        keep = ['credentials-global.json', 'checkin.log', 'custom.json.schedule-id']
        for name in keep:
            (self.root / name).write_text('123456789abc')
        with patch.object(Scheduler, 'status', return_value=False):
            self.assertEqual(self.run_delete(), 0)
            self.assertFalse(self.config.exists())
            self.assertEqual(self.run_delete(), 0)
        for name in keep:
            self.assertEqual((self.root / name).read_text(), '123456789abc')

    @patch.object(cli.sys, 'platform', 'darwin')
    def test_registered_or_unknown_scheduler_preserves_config(self):
        for result in [True, ScheduleError('cannot query')]:
            with self.subTest(result=result), patch.object(Scheduler, 'status') as status:
                if isinstance(result, Exception):
                    status.side_effect = result
                else:
                    status.return_value = result
                self.assertEqual(self.run_delete(), 2)
                self.assertTrue(self.config.exists())

    def test_active_run_or_management_preserves_config(self):
        for target in [self.config, self.config.with_name('custom.json.setup')]:
            with cli.run_lock(target), patch.object(Scheduler, 'status', return_value=False):
                self.assertEqual(self.run_delete(), 2)
                self.assertTrue(self.config.exists())
