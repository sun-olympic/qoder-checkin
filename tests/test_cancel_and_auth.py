import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import checkin_cli as cli
import qoder_checkin as q
from qoder_schedule import Scheduler


class CancellationAndAuthTests(unittest.TestCase):
    def setUp(self):
        probe = patch.object(Scheduler, 'preflight')
        probe.start()
        self.addCleanup(probe.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = self.root / 'credentials.json'
        self.original = b'{"token":"old-fake","account_id":"owner"}'
        self.token.write_bytes(self.original)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'accounts': [{'name': 'a', 'region': 'global',
            'token_file': 'credentials.json'}], 'schedule': {'at': '10:05'}}))

    def test_cancel_restores_config_and_returns_130(self):
        original = self.config.read_bytes()
        for point in ('checkin_cli.save_config', 'qoder_schedule.Scheduler.install'):
            with self.subTest(point=point), patch(point, side_effect=KeyboardInterrupt()), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(['install', '--config', str(self.config), '--at', '11:00']), 130)
            self.assertEqual(self.config.read_bytes(), original)
            self.assertFalse(list(self.root.glob('.config.json-*')))

    def test_macos_cancel_restores_service_and_definition(self):
        s = Scheduler(self.config, platform='darwin')
        s.plist = self.root / 'task.plist'
        s.plist.write_bytes(b'original-definition')
        active = True
        def command(args):
            nonlocal active
            if args[1] == 'bootout': active = False
            elif args[1] == 'bootstrap':
                if s.plist.read_bytes() != b'original-definition': raise KeyboardInterrupt()
                active = True
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch.object(s, 'status', side_effect=lambda: active), \
                patch.object(s, 'command', side_effect=command), \
                patch('qoder_schedule.os.getuid', return_value=501, create=True):
            with self.assertRaises(KeyboardInterrupt): s.install()
        self.assertTrue(active)
        self.assertEqual(s.plist.read_bytes(), b'original-definition')
        self.assertFalse(list(self.root.glob('.task.plist-*')))

    def test_windows_cancel_restores_previous_task(self):
        s = Scheduler(self.config, platform='win32')
        installed = []
        def register(definition):
            installed.append(definition)
            if len(installed) == 1: raise KeyboardInterrupt()
        with patch.object(s, 'definition', return_value=b'new'), \
                patch.object(s, 'status', return_value=True), \
                patch.object(s, 'command', return_value=subprocess.CompletedProcess([], 0, '<Task><Old /></Task>', '')), \
                patch.object(s, 'register_windows', side_effect=register):
            with self.assertRaises(KeyboardInterrupt): s.install()
        self.assertEqual(len(installed), 2)
        self.assertIn('Old', installed[1].decode('utf-16'))

    def sync(self, imported, failure=None):
        account = q.Account('a', 'global', q.BASES['global'], 'TEST_UNUSED_AUTH', self.token,
                            auth_source='macos', expected_account_id='owner')
        def importer(command, **kwargs):
            target = Path(command[command.index('--output') + 1])
            self.assertNotEqual(target, self.token)
            self.assertEqual(self.token.read_bytes(), self.original)
            target.write_text(json.dumps(imported))
            if failure: raise failure
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch.object(q.sys, 'platform', 'darwin'), \
                patch.object(q.shutil, 'which', return_value='node'), \
                patch.object(q.subprocess, 'run', side_effect=importer), \
                patch.dict(os.environ, {}, clear=True):
            account.sync_login()

    def test_wrong_account_preserves_original(self):
        with self.assertRaisesRegex(q.AuthError, '账号已改变'):
            self.sync({'token': 'new-fake', 'account_id': 'other'})
        self.assertEqual(self.token.read_bytes(), self.original)
        self.assertFalse(list(self.root.glob('.qoder-auth-*')))

    def test_matching_account_replaces_only_after_validation(self):
        self.sync({'token': 'new-fake', 'account_id': 'owner'})
        self.assertEqual(json.loads(self.token.read_text())['token'], 'new-fake')
        if os.name != 'nt': self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.root.glob('.qoder-auth-*')))

    def test_invalid_token_preserves_original(self):
        with self.assertRaises(q.ConfigError):
            self.sync({'token': 'invalid\nvalue', 'account_id': 'owner'})
        self.assertEqual(self.token.read_bytes(), self.original)
        self.assertFalse(list(self.root.glob('.qoder-auth-*')))

    def test_cancelled_import_preserves_original(self):
        with self.assertRaises(KeyboardInterrupt):
            self.sync({'token': 'new-fake', 'account_id': 'owner'}, KeyboardInterrupt())
        self.assertEqual(self.token.read_bytes(), self.original)
        self.assertFalse(list(self.root.glob('.qoder-auth-*')))


if __name__ == '__main__': unittest.main()
