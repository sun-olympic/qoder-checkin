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
                if args[-1] != 'login':
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
                    self.assertEqual(target.stat().st_mode & 0o777,0o600)

    def test_wrong_account_preserves_old_credential(self):
        self.exercise(account='other')

    def test_rejected_token_preserves_old_credential(self):
        self.exercise(reject=True)

    def test_validated_token_saved_privately(self):
        self.exercise()

    def test_cn_never_launches_global_login(self):
        with patch.object(auth,'ensure_cli') as launch:
            with self.assertRaises(q.ConfigError):
                auth.browser_login('cn',Path('unused.json'))
            launch.assert_not_called()
