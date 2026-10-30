import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import qoder_cli_auth as auth
import qoder_checkin as q


class OfficialLoginTests(unittest.TestCase):
    def test_isolated_login_does_not_reuse_shared_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'auth-global/.auth').mkdir(parents=True)
            (root/'auth-global/.auth/user').touch()
            def command(args, **kwargs):
                if args[-1].endswith('.json'):  # only the credential importer writes a file
                    Path(args[-1]).write_text(json.dumps({'token':'fake','account_id':'new'}))
                else:
                    self.assertEqual(Path(args[2]).resolve(), (root/'cli-session').resolve())
                return SimpleNamespace(returncode=0)
            with patch.object(auth,'RUNTIME',root), patch.object(auth,'ensure_cli',return_value=root/'qodercli'), \
                    patch.object(auth.shutil,'which',return_value='node'), \
                    patch.object(auth.subprocess,'run',side_effect=command) as run, \
                    patch.object(q.CampaignClient,'campaigns',return_value=[]):
                auth.browser_login('global', root/'credential.json', isolated=True)
            self.assertEqual(run.call_count, 2)

    def exercise(self, account='owner', reject=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'auth-global/.auth').mkdir(parents=True)
            (root/'auth-global/.auth/user').touch()
            target = root/'credential.json'
            target.write_text('old credential')
            def importer(command, **kwargs):
                if command[-1].endswith('.json'):  # login/status calls must not write a file in the cwd
                    Path(command[-1]).write_text(json.dumps({'token':'test-token',
                        'account_id':account,'expiresAt':'2099-01-01T00:00:00Z'}))
                return SimpleNamespace(returncode=0)
            with patch.object(auth,'RUNTIME',root), patch.object(auth,'ensure_cli',return_value=root/'qodercli'), \
                 patch.object(auth.shutil,'which',return_value='node'), \
                 patch.object(auth.subprocess,'run',side_effect=importer), \
                 patch.object(q.CampaignClient,'campaigns',side_effect=q.AuthError('rejected') if reject else None,return_value=[]):
                if account != 'owner' or reject:
                    with self.assertRaises(q.AuthError):
                        auth.browser_login('global',target,expected_account_id='owner')
                    self.assertEqual(target.read_text(),'old credential')
                else:
                    auth.browser_login('global',target,expected_account_id='owner')
                    self.assertEqual(json.loads(target.read_text())['account_id'],'owner')
                    if os.name != 'nt':
                        self.assertEqual(target.stat().st_mode & 0o777,0o600)

    def test_wrong_account_preserves_old_credential(self):
        self.exercise(account='other')

    def test_rejected_token_preserves_old_credential(self):
        self.exercise(reject=True)

    def test_validated_token_saved_privately(self):
        self.exercise()

    def relogin(self, with_session, expiries, **options):
        """Run browser_login with a fake CLI; `expiries` feeds each successive credential import.

        Returns (events, saved expiresAt or None, error or None, session dirs passed to the CLI)."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            own = Path(options['session_dir']) if 'session_dir' in options else root/'auth-global'
            session = own/'.auth/user'
            session.parent.mkdir(parents=True)
            if with_session:
                session.touch()
            events, exports, dirs = [], iter(expiries), set()
            def command(args, **kwargs):
                if args[-1] == 'login':
                    events.append(('login', session.exists()))
                    session.touch()
                elif args[-1] == 'status':
                    events.append(('status',))
                else:
                    events.append(('import',))
                    Path(args[-1]).write_text(json.dumps({'token':'t','account_id':'owner',
                                                          'expiresAt':next(exports)}))
                if '--config-dir' in args:
                    dirs.add(args[args.index('--config-dir') + 1])
                return SimpleNamespace(returncode=0)
            with patch.object(auth,'RUNTIME',root), patch.object(auth,'ensure_cli',return_value=root/'qodercli'), \
                 patch.object(auth.shutil,'which',return_value='node'), \
                 patch.object(auth.subprocess,'run',side_effect=command), \
                 patch.object(q.CampaignClient,'campaigns',return_value=[]):
                target = root/'credential.json'
                try:
                    auth.browser_login('global', target, **options)
                    saved, error = json.loads(target.read_text())['expiresAt'], None
                except q.AuthError as exc:
                    saved, error = None, str(exc)
            return events, saved, error, dirs

    def test_expired_cached_session_triggers_fresh_browser_login(self):
        events, saved, _, _ = self.relogin(True, ['2020-01-01T00:00:00Z', '2099-01-01T00:00:00Z'])
        # cached session: CLI `status` first (lets it refresh), import; stale -> cleared (False = gone), one re-login
        self.assertEqual(events, [('status',), ('import',), ('login', False), ('import',)])
        self.assertEqual(saved, '2099-01-01T00:00:00Z')

    def test_fresh_login_with_expired_token_is_not_retried(self):
        events, saved, _, _ = self.relogin(False, ['2020-01-01T00:00:00Z'])
        self.assertEqual(events, [('login', False), ('import',)])
        self.assertIsNone(saved)

    def test_browser_prompt_only_when_a_browser_is_actually_needed(self):
        import contextlib, io
        prompt = '请在系统浏览器完成 Qoder 授权'
        with contextlib.redirect_stdout(io.StringIO()) as silent:   # cached, valid session: no browser
            self.relogin(True, ['2099-01-01T00:00:00Z'])
        with contextlib.redirect_stdout(io.StringIO()) as fresh:    # no session: browser login
            self.relogin(False, ['2099-01-01T00:00:00Z'])
        self.assertNotIn(prompt, silent.getvalue())
        self.assertIn(prompt, fresh.getvalue())

    def test_noninteractive_renews_from_cached_session_without_browser(self):
        events, saved, _, _ = self.relogin(True, ['2099-01-01T00:00:00Z'], interactive=False)
        self.assertEqual(events, [('status',), ('import',)])
        self.assertEqual(saved, '2099-01-01T00:00:00Z')

    def test_noninteractive_never_opens_browser(self):
        # no cached session -> refuse before running anything; unusable cached session -> raise, no re-login
        events, saved, error, _ = self.relogin(False, [], interactive=False)
        self.assertEqual(events, [])
        self.assertIn('没有可用的官方 CLI 登录态', error)
        events, saved, error, _ = self.relogin(True, ['2020-01-01T00:00:00Z'], interactive=False)
        self.assertEqual(events, [('status',), ('import',)])
        self.assertIsNone(saved)
        self.assertIn('已过期', error)

    def test_session_dir_is_used_as_cli_config_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            own = Path(directory)/'sessions'/'acct'
            events, saved, _, dirs = self.relogin(True, ['2099-01-01T00:00:00Z'], session_dir=own, interactive=False)
            self.assertEqual(dirs, {str(own)})
            self.assertEqual(saved, '2099-01-01T00:00:00Z')

    def test_cn_never_launches_global_login(self):
        with patch.object(auth,'ensure_cli') as launch:
            with self.assertRaises(q.ConfigError):
                auth.browser_login('cn',Path('unused.json'))
            launch.assert_not_called()
