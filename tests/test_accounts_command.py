import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import checkin_cli as cli


def make_config(tmp, rows):
    path = Path(tmp) / 'config.json'
    cli.save_config(path, {'accounts': rows})
    return path


def row(name, **extra):
    return {'name': name, 'region': 'global', 'token_file': f'{name}.json', **extra}


def run(args, inputs=()):
    out = io.StringIO()
    with patch('builtins.input', side_effect=list(inputs)), contextlib.redirect_stdout(out):
        code = cli.main(args)
    return code, out.getvalue()


class AccountsCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def config(self, rows):
        return make_config(self.dir, rows), rows

    def saved(self, path):
        return json.loads(path.read_text())['accounts']

    def staging(self):
        return list(self.dir.glob('.qoder-add-*'))

    def test_list_shows_status_and_hides_token(self):
        path, _ = self.config([row('ok', expected_account_id='uid-1', display_name='主账号'), row('old')])
        cli.q.private_json(self.dir / 'ok.json', {'token': 'SECRET', 'expiresAt': '2099-01-01T00:00:00Z'})
        cli.q.private_json(self.dir / 'old.json', {'token': 'SECRET', 'expiresAt': '2020-01-01T00:00:00Z'})
        code, out = run(['accounts', 'list', '--config', str(path)])
        self.assertEqual(code, 0)
        self.assertIn('主账号', out)
        self.assertIn('uid-1', out)
        self.assertIn('已过期', out)
        self.assertNotIn('SECRET', out)

    def test_rename_sets_label_and_empty_restores_nickname(self):
        path, _ = self.config([row('a'), row('b')])
        self.assertEqual(run(['accounts', 'rename', 'a', '新名字', '--config', str(path)])[0], 0)
        self.assertEqual(self.saved(path)[0]['display_name'], '新名字')
        run(['accounts', 'rename', '新名字', '--config', str(path)], inputs=[''])  # match by label, prompt for new one
        self.assertEqual(self.saved(path)[0]['display_name'], '')
        self.assertEqual(run(['accounts', 'rename', 'a', 'x' * 65, '--config', str(path)])[0], 2)

    def test_target_can_be_the_bound_account_id_but_only_exactly(self):
        path, _ = self.config([row('a', expected_account_id='uid-1'), row('b', expected_account_id='uid-2')])
        self.assertEqual(run(['accounts', 'rename', 'uid-2', '二号', '--config', str(path)])[0], 0)
        self.assertEqual(self.saved(path)[1]['display_name'], '二号')
        before = path.read_text()
        self.assertEqual(run(['accounts', 'rename', 'uid', 'x', '--config', str(path)])[0], 2)  # no prefix match
        self.assertEqual(path.read_text(), before)

    def test_shared_bound_account_id_is_ambiguous(self):
        path, _ = self.config([row('a', expected_account_id='uid-1'),
                               row('b', region='cn', token_file='b.json', expected_account_id='uid-1')])
        before = path.read_text()
        self.assertEqual(run(['accounts', 'rename', 'uid-1', 'x', '--config', str(path)])[0], 2)
        self.assertEqual(path.read_text(), before)

    def test_ambiguous_or_unknown_target_changes_nothing(self):
        path, _ = self.config([row('a', display_name='same'), row('b', display_name='same')])
        before = path.read_text()
        for key in ('same', 'nope'):
            self.assertEqual(run(['accounts', 'rename', key, 'x', '--config', str(path)])[0], 2)
        self.assertEqual(path.read_text(), before)

    def test_remove_requires_confirmation_and_deletes_unshared_credential_only_on_yes(self):
        path, _ = self.config([row('a'), row('b')])
        for name in ('a', 'b'):
            cli.q.private_json(self.dir / f'{name}.json', {'token': 't'})
        run(['accounts', 'remove', 'a', '--config', str(path)], inputs=['n'])
        self.assertEqual(len(self.saved(path)), 2)
        run(['accounts', 'remove', 'a', '--config', str(path)], inputs=['y', 'n'])  # keep credential file
        self.assertEqual([r['name'] for r in self.saved(path)], ['b'])
        self.assertTrue((self.dir / 'a.json').exists())

    def test_remove_deletes_credential_when_asked(self):
        path, _ = self.config([row('a'), row('b')])
        cli.q.private_json(self.dir / 'a.json', {'token': 't'})
        run(['accounts', 'remove', 'a', '--config', str(path)], inputs=['y', 'y'])
        self.assertFalse((self.dir / 'a.json').exists())

    def test_remove_never_deletes_credential_shared_with_another_account(self):
        path, _ = self.config([row('a', token_file='shared.json'), row('b', token_file='shared.json')])
        cli.q.private_json(self.dir / 'shared.json', {'token': 't'})
        run(['accounts', 'remove', 'a', '--config', str(path)], inputs=['y'])  # no second prompt expected
        self.assertTrue((self.dir / 'shared.json').exists())

    def test_remove_drops_own_cli_session_but_not_a_shared_one(self):
        runtime = self.dir / 'runtime'
        with patch.object(cli.qoder_cli_auth, 'RUNTIME', runtime):
            for stem in ('a', 'shared'):
                (runtime / 'sessions' / stem).mkdir(parents=True)
            path, _ = self.config([row('a'), row('b', token_file='shared.json'), row('c', token_file='shared.json')])
            run(['accounts', 'remove', 'a', '--config', str(path)], inputs=['y'])
            self.assertFalse((runtime / 'sessions/a').exists())
            run(['accounts', 'remove', 'b', '--config', str(path)], inputs=['y'])
            self.assertTrue((runtime / 'sessions/shared').exists())  # still used by account c

    def test_cannot_remove_last_account(self):
        path, _ = self.config([row('only')])
        self.assertEqual(run(['accounts', 'remove', 'only', '--config', str(path)], inputs=['y'])[0], 2)
        self.assertEqual(len(self.saved(path)), 1)

    def refresh(self, rows, login):
        path, _ = self.config(rows)
        with patch.object(cli.qoder_auth, 'browser_login', side_effect=login) as mocked:
            code, _ = run(['accounts', 'refresh', rows[0]['name'], '--config', str(path)])
        return path, code, mocked

    def test_refresh_uses_isolated_login_bound_identity_and_replaces_credential(self):
        cli.q.private_json(self.dir / 'a.json', {'token': 'old'})
        def login(region, output, **kwargs):
            self.assertTrue(kwargs['isolated'])
            self.assertEqual(kwargs['expected_account_id'], 'uid-1')
            # own persistent session (keeps the refresh token), named after the credential file
            self.assertEqual(Path(kwargs['session_dir']).parts[-2:], ('sessions', 'a'))
            cli.q.private_json(output, {'token': 'new', 'account_id': 'uid-1'})
            return {'account_id': 'uid-1'}
        path, code, _ = self.refresh([row('a', expected_account_id='uid-1')], login)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((self.dir / 'a.json').read_text())['token'], 'new')
        self.assertFalse(self.staging())

    def refresh_message(self, old_days, new_days):
        from datetime import datetime, timedelta, timezone
        iso = lambda d: (datetime.now(timezone.utc) + timedelta(days=d)).strftime('%Y-%m-%dT%H:%M:%SZ')
        (self.dir / 'a.json').write_text(json.dumps({'token': 't', 'account_id': 'uid-1', 'expiresAt': iso(old_days)}))
        path, _ = self.config([row('a', expected_account_id='uid-1')])
        def login(region, output, **kwargs):
            Path(output).write_text(json.dumps({'token': 't', 'account_id': 'uid-1', 'expiresAt': iso(new_days)}))
            return {'account_id': 'uid-1'}
        with patch.object(cli.qoder_auth, 'browser_login', side_effect=login):
            return run(['accounts', 'refresh', 'a', '--config', str(path)])[1]

    def test_refresh_says_renewed_only_when_expiry_actually_moved(self):
        self.assertIn('已续期', self.refresh_message(old_days=-1, new_days=30))
        unchanged = self.refresh_message(old_days=20, new_days=20)
        self.assertIn('无需续期', unchanged)
        self.assertNotIn('已续期', unchanged)

    def relogin(self, login):
        """accounts refresh --relogin with an existing own session (marker 'old') under a temp RUNTIME."""
        runtime = self.dir / 'runtime'
        own = runtime / 'sessions' / 'a'
        (own / '.auth').mkdir(parents=True)
        (own / '.auth/user').write_text('old')
        cli.q.private_json(self.dir / 'a.json', {'token': 'old', 'account_id': 'uid-1'})
        path, _ = self.config([row('a', expected_account_id='uid-1')])
        with patch.object(cli.qoder_cli_auth, 'RUNTIME', runtime), \
                patch.object(cli.qoder_auth, 'browser_login', side_effect=login) as mocked:
            code, out = run(['accounts', 'refresh', 'a', '--relogin', '--config', str(path)])
        return code, out, own, mocked

    def test_relogin_forces_fresh_login_then_swaps_in_the_new_session(self):
        def login(region, output, **kwargs):
            staging = Path(kwargs['session_dir'])
            self.assertTrue(staging.name.endswith('.new'))      # never reuses the cached session
            self.assertFalse(staging.exists())
            (staging / '.auth').mkdir(parents=True)
            (staging / '.auth/user').write_text('new')
            Path(output).write_text(json.dumps({'token': 'new', 'account_id': 'uid-1'}))
            return {'account_id': 'uid-1'}
        code, _, own, _ = self.relogin(login)
        self.assertEqual(code, 0)
        self.assertEqual((own / '.auth/user').read_text(), 'new')
        self.assertEqual(json.loads((self.dir / 'a.json').read_text())['token'], 'new')
        self.assertEqual(sorted(p.name for p in own.parent.iterdir()), ['a'])  # no .new / .old leftovers

    def test_relogin_failure_keeps_old_session_and_credential(self):
        def login(region, output, **kwargs):
            (Path(kwargs['session_dir']) / '.auth').mkdir(parents=True)
            raise cli.q.AuthError('登录账号与绑定账号不一致；原凭证未修改')
        code, _, own, _ = self.relogin(login)
        self.assertEqual(code, 2)
        self.assertEqual((own / '.auth/user').read_text(), 'old')
        self.assertEqual(json.loads((self.dir / 'a.json').read_text())['token'], 'old')
        self.assertEqual(sorted(p.name for p in own.parent.iterdir()), ['a'])

    def relogin_unverified_by_network(self, bound=True):
        """The browser login succeeded but the confirmation request hit a network failure."""
        def login(region, output, **kwargs):
            staging = Path(kwargs['session_dir'])
            (staging / '.auth').mkdir(parents=True)
            (staging / '.auth/user').write_text('new')
            raise cli.q.ApiError('网络请求失败或超时；请检查网络、代理和证书')
        runtime = self.dir / 'runtime'
        own = runtime / 'sessions' / 'a'
        (own / '.auth').mkdir(parents=True)
        (own / '.auth/user').write_text('old')
        cli.q.private_json(self.dir / 'a.json', {'token': 'old', 'account_id': 'uid-1'})
        path, _ = self.config([row('a', **({'expected_account_id': 'uid-1'} if bound else {}))])
        err = io.StringIO()
        with patch.object(cli.qoder_cli_auth, 'RUNTIME', runtime), \
                patch.object(cli.qoder_auth, 'browser_login', side_effect=login), contextlib.redirect_stderr(err):
            code, _ = run(['accounts', 'refresh', 'a', '--relogin', '--config', str(path)])
        return code, err.getvalue(), own

    def test_relogin_network_failure_keeps_the_authorized_session_not_the_credential(self):
        code, err, own = self.relogin_unverified_by_network()
        self.assertEqual(code, 2)
        self.assertEqual((own / '.auth/user').read_text(), 'new')            # authorization is not thrown away
        self.assertEqual(json.loads((self.dir / 'a.json').read_text())['token'], 'old')  # credential untouched
        self.assertEqual(sorted(p.name for p in own.parent.iterdir()), ['a'])  # no .new / .old leftovers
        self.assertIn('无需再次授权', err)
        self.assertIn('accounts refresh', err)

    def test_relogin_network_failure_discards_session_when_identity_was_never_bound(self):
        code, _, own = self.relogin_unverified_by_network(bound=False)
        self.assertEqual(code, 2)
        self.assertEqual((own / '.auth/user').read_text(), 'old')            # nothing proves it is the same account
        self.assertEqual(sorted(p.name for p in own.parent.iterdir()), ['a'])

    def test_plain_refresh_still_reuses_the_cached_session(self):
        seen = []
        def login(region, output, **kwargs):
            seen.append(Path(kwargs['session_dir']).name)
            Path(output).write_text(json.dumps({'token': 'x', 'account_id': 'uid-1'}))
            return {'account_id': 'uid-1'}
        runtime = self.dir / 'runtime'
        path, _ = self.config([row('a', expected_account_id='uid-1')])
        with patch.object(cli.qoder_cli_auth, 'RUNTIME', runtime), \
                patch.object(cli.qoder_auth, 'browser_login', side_effect=login):
            run(['accounts', 'refresh', 'a', '--config', str(path)])
        self.assertEqual(seen, ['a'])

    def test_refresh_wrong_account_keeps_old_credential(self):
        cli.q.private_json(self.dir / 'a.json', {'token': 'old'})
        def login(region, output, **kwargs):
            raise cli.q.AuthError('登录账号与绑定账号不一致；原凭证未修改')
        _, code, _ = self.refresh([row('a', expected_account_id='uid-1')], login)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads((self.dir / 'a.json').read_text())['token'], 'old')
        self.assertFalse(self.staging())

    def test_refresh_binds_identity_for_unbound_account(self):
        def login(region, output, **kwargs):
            cli.q.private_json(output, {'token': 'new', 'account_id': 'uid-9'})
            return {'account_id': 'uid-9'}
        path, code, _ = self.refresh([row('a')], login)
        self.assertEqual(code, 0)
        self.assertEqual(self.saved(path)[0]['expected_account_id'], 'uid-9')

    def test_unknown_action_and_stray_args_rejected(self):
        path, _ = self.config([row('a')])
        self.assertEqual(run(['accounts', 'bogus', '--config', str(path)])[0], 2)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(['doctor', 'extra', '--config', str(path)])
        for argv in (['doctor', '--relogin'], ['accounts', 'list', '--relogin'], ['accounts', 'remove', 'a', '--relogin']):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cli.main(argv + ['--config', str(path)])  # --relogin only belongs to `accounts refresh`


if __name__ == '__main__':
    unittest.main()
