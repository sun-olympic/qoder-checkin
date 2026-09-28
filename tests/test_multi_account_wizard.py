import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import checkin_cli as cli


class MultiAccountWizardTests(unittest.TestCase):
    def test_duplicate_browser_identity_does_not_publish_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'accounts': [{'name':'old','region':'global','expected_account_id':'uid'}]}
            def login(region, output, **kwargs):
                cli.q.private_json(output, {'token':'fake','account_id':'uid'})
                return {'account_id':'uid'}
            with patch('builtins.input', side_effect=['a','new','global','1']), \
                    patch.object(cli.qoder_auth, 'browser_login', side_effect=login):
                with self.assertRaises(cli.q.ConfigError):
                    cli.wizard_accounts(config, Path(tmp))
            self.assertEqual(len(config['accounts']), 1)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_wizard_persists_addition_and_preserves_old_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'config.json'
            original = {'name':'old','region':'global','token_file':'old.json'}
            cli.save_config(path, {'accounts':[original], 'schedule':{'at':'18:00'}})
            with patch('builtins.input', side_effect=['a','work','global','2','','','1']), \
                    patch.object(cli, 'getpass', return_value='new-token'), \
                    patch('checkin_cli.sys.stdin.isatty', return_value=False):
                self.assertEqual(cli.main(['wizard','--config',str(path)]), 0)
            saved = cli.read_config(path)
            self.assertEqual(saved['accounts'][0], original)
            self.assertEqual(saved['accounts'][1]['name'], 'work')
            self.assertEqual(saved['schedule'], {'at':'18:00','region':'both'})

    def test_add_two_same_region_accounts_with_separate_credentials(self):
        self.assertTrue(hasattr(cli, 'wizard_accounts'))
        with tempfile.TemporaryDirectory() as tmp:
            config = {'accounts': []}
            with patch('builtins.input', side_effect=['a','personal','global','2',
                                                      'a','work','global','2','']), \
                    patch.object(cli, 'getpass', side_effect=['token-one','token-two']):
                cli.wizard_accounts(config, Path(tmp))
            rows = config['accounts']
            self.assertEqual([r['name'] for r in rows], ['personal','work'])
            self.assertNotEqual(rows[0]['token_file'], rows[1]['token_file'])
            self.assertNotEqual(rows[0]['token_env'], rows[1]['token_env'])
            self.assertIn('token-one', (Path(tmp)/rows[0]['token_file']).read_text())

    def test_duplicate_name_rejected_before_login(self):
        self.assertTrue(hasattr(cli, 'wizard_accounts'))
        config = {'accounts': [{'name':'work','region':'global'}]}
        with patch('builtins.input', side_effect=['a','work']), \
                patch.object(cli.qoder_auth, 'browser_login') as login:
            with self.assertRaises(cli.q.ConfigError):
                cli.wizard_accounts(config, Path('/tmp'))
        login.assert_not_called()

    def test_browser_add_uses_isolated_login_and_binds_identity(self):
        self.assertTrue(hasattr(cli, 'wizard_accounts'))
        with tempfile.TemporaryDirectory() as tmp:
            config = {'accounts': []}
            def login(region, output, **kwargs):
                self.assertTrue(kwargs['isolated'])
                cli.q.private_json(output, {'token':'fake','account_id':'uid'})
                return {'account_id':'uid'}
            with patch('builtins.input', side_effect=['a','work','global','1','']), \
                    patch.object(cli.qoder_auth, 'browser_login', side_effect=login):
                cli.wizard_accounts(config, Path(tmp))
            self.assertEqual(config['accounts'][0]['expected_account_id'], 'uid')
