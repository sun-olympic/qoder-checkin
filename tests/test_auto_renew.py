import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import checkin_cli as cli


def iso(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


class AutoRenewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.runtime = self.dir / 'runtime'
        patcher = patch.object(cli.qoder_cli_auth, 'RUNTIME', self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []

    def setup_account(self, days, session=None, **row):
        """One global account whose credential expires in `days`; session: 'own' | 'shared' | None."""
        cred = self.dir / 'cred.json'
        cred.write_text(json.dumps({'token': 't', 'account_id': 'uid-1', 'expiresAt': iso(days)}))
        base = {'name': 'a', 'region': 'global', 'token_file': 'cred.json', 'expected_account_id': 'uid-1'}
        self.config = self.dir / 'config.json'
        cli.save_config(self.config, {'accounts': [{**base, **row}]})
        if session:
            home = self.runtime / ('sessions/cred' if session == 'own' else 'auth-global')
            (home / '.auth').mkdir(parents=True, exist_ok=True)
            (home / '.auth/user').touch()
        return cred

    def renew(self, outcome=None, **kw):
        """Run renew_expiring with a fake CLI login; outcome=None renews to +30d, an exception is raised."""
        def login(region, output, **kwargs):
            self.calls.append(kwargs)
            if outcome is not None:
                raise outcome
            # the real login replaces the existing credential file
            Path(output).write_text(json.dumps({'token': 'new', 'account_id': 'uid-1', 'expiresAt': iso(30)}))
        with patch.object(cli.qoder_auth, 'browser_login', side_effect=login), \
                contextlib.redirect_stdout(io.StringIO()):
            return cli.renew_expiring(self.config, 'both', **kw)

    def test_healthy_credential_is_left_alone(self):
        self.setup_account(20, session='own')
        self.assertEqual(self.renew(), [])
        self.assertEqual(self.calls, [])

    def test_expiring_credential_renewed_silently_from_own_session(self):
        cred = self.setup_account(1, session='own')
        self.assertEqual(self.renew(), [])
        call = self.calls[0]
        self.assertFalse(call['interactive'])
        self.assertEqual(call['session_dir'], self.runtime / 'sessions/cred')
        self.assertEqual(call['expected_account_id'], 'uid-1')
        self.assertEqual(json.loads(cred.read_text())['token'], 'new')

    def test_falls_back_to_shared_login_session(self):
        self.setup_account(-1, session='shared')
        self.assertEqual(self.renew(), [])
        self.assertEqual(self.calls[0]['session_dir'], self.runtime / 'auth-global')

    def test_failed_renewal_reminds_and_keeps_credential(self):
        cred = self.setup_account(-1, session='own')
        reminders = self.renew(cli.q.AuthError('登录凭证已过期'))
        self.assertEqual(len(reminders), 1)
        self.assertIn('已过期', reminders[0])
        self.assertIn('accounts refresh a', reminders[0])
        self.assertEqual(json.loads(cred.read_text())['token'], 't')

    def test_expiring_but_unrenewable_reminds_without_trying(self):
        self.setup_account(2)  # no CLI session anywhere
        reminders = self.renew()
        self.assertEqual(self.calls, [])
        self.assertIn('即将到期', reminders[0])

    def test_unexpected_cli_errors_do_not_escape(self):
        self.setup_account(-1, session='own')
        self.assertEqual(len(self.renew(OSError('disk'))), 1)

    def test_env_override_and_macos_source_are_skipped(self):
        self.setup_account(-1, session='own', token_env='QODER_TEST_RENEW_TOKEN')
        with patch.dict(os.environ, {'QODER_TEST_RENEW_TOKEN': 'abc'}):
            self.assertEqual(self.renew(), [])
        self.setup_account(-1, session='own', auth_source='macos')
        self.assertEqual(self.renew(), [])
        self.assertEqual(self.calls, [])

    def run_command(self, reminders, dry_run=False):
        args = SimpleNamespace(config=self.config, region='both', dry_run=dry_run)
        with patch.object(cli, 'renew_expiring', return_value=reminders), \
                patch.object(cli.q, 'run_once', return_value=[]) as claim, \
                patch.object(cli.q, 'emit'), patch.object(cli.q, 'exit_code', return_value=0), \
                patch.object(cli.notify, 'notify_results', return_value='disabled'), \
                patch.object(cli.notify, 'validate', return_value='wx_test'), \
                patch.object(cli.notify, 'send') as send, \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli.run(args)
        return code, claim, send

    def test_run_still_claims_and_pushes_reminder_when_renewal_fails(self):
        self.setup_account(20)
        code, claim, send = self.run_command(['账号「a」凭证已过期'])
        self.assertEqual(code, 0)
        claim.assert_called_once()
        send.assert_called_once()
        self.assertIn('凭证已过期', send.call_args.args[2])

    def test_dry_run_and_no_reminders_push_nothing(self):
        self.setup_account(20)
        self.assertEqual(self.run_command(['x'], dry_run=True)[2].call_count, 0)
        self.assertEqual(self.run_command([])[2].call_count, 0)


if __name__ == '__main__':
    unittest.main()
