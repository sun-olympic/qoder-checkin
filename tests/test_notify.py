import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import qoder_notify as n
import checkin_cli as cli


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.json'
        self.cfg = dict(zip(n.FIELDS, ['app', 'SECRET', 'user', 'tpl']))
        self.cfg['notify_channel'] = 'wx_test'
        self.rows = [{'account':'global', 'region':'global', 'result':'claimed',
                      'campaign_id':'daily', 'rewardCredits':100}]

    def test_disabled_no_network_or_state(self):
        with patch.object(n, 'send') as send:
            self.assertEqual(n.notify_results({}, self.path, self.rows), 'disabled')
            send.assert_not_called()
        self.assertFalse(list(self.path.parent.glob('*.notify-state.json')))

    def test_dedupe_success_already_claimed_and_new_campaign(self):
        with patch.object(n, 'send') as send:
            self.assertEqual(n.notify_results(self.cfg, self.path, self.rows), 'accepted')
            self.rows[0]['result'] = 'already_claimed'
            self.assertEqual(n.notify_results(self.cfg, self.path, self.rows), 'duplicate')
            self.rows[0]['campaign_id'] = 'new'
            n.notify_results(self.cfg, self.path, self.rows)
            self.assertEqual(send.call_count, 2)
        state = self.path.with_name('config.json.notify-state.json')
        self.assertNotIn('SECRET', state.read_text())
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)

    def test_recovery_and_recipient_change_not_suppressed(self):
        with patch.object(n, 'send') as send:
            failed = [{**self.rows[0], 'result':'error', 'message':'secret token raw response'}]
            n.notify_results(self.cfg, self.path, failed)
            self.assertNotIn('secret token', send.call_args.args[2])
            n.notify_results(self.cfg, self.path, self.rows)
            self.cfg['wx_test_touser'] = 'other'
            n.notify_results(self.cfg, self.path, self.rows)
            self.assertEqual(send.call_count, 3)

    def test_failure_does_not_mark_delivered(self):
        with patch.object(n, 'send', side_effect=n.NotifyError('failed')):
            with self.assertRaises(n.NotifyError):
                n.notify_results(self.cfg, self.path, self.rows)
        with patch.object(n, 'send') as send:
            n.notify_results(self.cfg, self.path, self.rows)
            send.assert_called_once()

    def test_api_business_error_is_failure(self):
        with patch.object(n, 'request', side_effect=[{'access_token':'token'}, {'errcode':40003,'errmsg':'SECRET'}]):
            with self.assertRaisesRegex(n.NotifyError, '40003') as cm:
                n.send(self.cfg, 'title', 'content')
            self.assertNotIn('SECRET', str(cm.exception))

    def test_explicit_token_rejection_refreshes_once(self):
        with patch.object(n, 'request', side_effect=[{'access_token':'old'}, {'errcode':42001},
                                                    {'access_token':'new'}, {'errcode':0}]) as call:
            n.send(self.cfg, 'title', 'content')
            self.assertEqual(call.call_count, 4)
            self.assertEqual(call.call_args.args[1]['data']['keyword2']['value'], 'content')

    def test_network_failure_not_retried_or_exposed(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=OSError('SECRET')) as op:
            with self.assertRaises(n.NotifyError) as cm:
                n.send(self.cfg, 'title', 'content')
            self.assertNotIn('SECRET', str(cm.exception))
            op.assert_called_once()

    def test_redirect_rejected(self):
        with self.assertRaises(n.NotifyError):
            n.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.test')

    def test_dry_run_no_push_and_notification_failure_no_reclaim(self):
        args = SimpleNamespace(config=self.path, region='global', dry_run=True)
        with patch.object(cli, 'read_config', return_value=self.cfg), patch.object(cli.q, 'run_once', return_value=self.rows) as run, patch.object(n, 'notify_results', side_effect=n.NotifyError('failed')) as push, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.run(args), 0)
            push.assert_not_called()
            args.dry_run = False
            self.assertEqual(cli.run(args), 1)
            self.assertEqual(run.call_count, 2)
            push.assert_called_once()

    def test_setup_preserves_existing_config(self):
        original = {'accounts':[{'name':'global'}], 'schedule':{'at':'10:05'}, 'other':True}
        with patch.object(cli, 'read_config', return_value=original), patch.object(cli, 'getpass', side_effect=['app','secret','user','tpl']), patch('builtins.input', side_effect=['2', 'n']), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wx_setup(SimpleNamespace(config=self.path)), 0)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['schedule'], original['schedule'])
        self.assertEqual(saved['accounts'], original['accounts'])
        self.assertTrue(saved['other'])
        self.assertEqual(saved['notify_channel'], 'wx_test')

    def test_bad_state_fails_closed(self):
        self.path.with_name('config.json.notify-state.json').write_text('[]')
        with patch.object(n, 'send') as send:
            with self.assertRaises(n.NotifyError):
                n.notify_results(self.cfg, self.path, self.rows)
            send.assert_not_called()

    def test_main_wizard_notification_choices_and_cancel_are_atomic(self):
        original = {**self.cfg, 'accounts':[], 'schedule':{'at':'10:05'}}
        for choice in ('1', '2', '3', 'invalid'):
            cli.save_config(self.path, original)
            with patch.object(cli, 'read_config', return_value=json.loads(json.dumps(original))), patch('builtins.input', side_effect=['11:00', choice, '2', 'n']), patch.object(cli, 'getpass', return_value=''), contextlib.redirect_stdout(io.StringIO()):
                if choice == 'invalid':
                    with self.assertRaises(cli.q.ConfigError):
                        cli.wizard(SimpleNamespace(config=self.path))
                    self.assertEqual(json.loads(self.path.read_text()), original)
                else:
                    cli.wizard(SimpleNamespace(config=self.path))
                    saved = json.loads(self.path.read_text())
                    self.assertEqual(saved['notify_channel'], 'none' if choice == '3' else 'wx_test')
                    self.assertEqual(saved['wx_test_secret'], 'SECRET')
                    self.assertEqual(saved['schedule']['at'], '11:00')
        cli.save_config(self.path, original)
        with patch.object(cli, 'read_config', return_value=json.loads(json.dumps(original))), patch('builtins.input', side_effect=['11:00', '2']), patch.object(cli, 'getpass', side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                cli.wizard(SimpleNamespace(config=self.path))
        self.assertEqual(json.loads(self.path.read_text()), original)

    def test_binding_discovers_only_matching_templates(self):
        with patch.object(n, 'request', side_effect=[{'access_token':'token'},
            {'total':1,'data':{'openid':['user']}},
            {'template_list':[{'template_id':'good','content':n.TEMPLATE},
                              {'template_id':'bad','content':'other template'}]}]):
            self.assertEqual(n.binding_options(self.cfg), (['user'], ['good']))

    def test_binding_rejects_partial_followers_and_api_error(self):
        for followers in ({'total':2,'data':{'openid':['user']}}, {'errcode':40013}):
            with patch.object(n, 'request', side_effect=[{'access_token':'token'}, followers]):
                with self.assertRaises(n.NotifyError):
                    n.binding_options(self.cfg)

    def test_auto_binding_preserves_account_and_requires_recipient_choice(self):
        cfg = {'accounts':[], 'schedule':{'at':'10:05'}}
        with patch.object(cli, 'getpass', side_effect=['app','secret']), patch('builtins.input', side_effect=['1','','2']), patch.object(n, 'binding_options', return_value=(['first','second'], ['tpl'])), contextlib.redirect_stdout(io.StringIO()):
            result = cli.configure_wechat(cfg)
        self.assertEqual(result['wx_test_touser'], 'second')
        self.assertEqual(result['wx_test_template_id'], 'tpl')
        self.assertEqual(result['schedule'], cfg['schedule'])
        self.assertNotIn('notify_channel', cfg)

    def test_test_notification_only_on_explicit_yes(self):
        with patch('builtins.input', return_value=''), patch.object(n, 'send') as send:
            cli.offer_notification_test(self.cfg)
            send.assert_not_called()
        with patch('builtins.input', return_value='y'), patch.object(n, 'send') as send, contextlib.redirect_stdout(io.StringIO()):
            cli.offer_notification_test(self.cfg)
            send.assert_called_once()
