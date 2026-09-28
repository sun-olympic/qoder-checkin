import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import qoder_checkin as q
import checkin_cli as cli


class AccountNameTests(unittest.TestCase):
    def test_custom_name_then_credential_nickname_then_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            credential = root/'token.json'
            credential.write_text(json.dumps({'token':'fake', 'nickname':'登录昵称'}))
            config = root/'config.json'
            row = {'name':'global','region':'global','token_file':'token.json'}
            for custom, expected in [('我的账号','我的账号'), ('','登录昵称')]:
                config.write_text(json.dumps({'accounts':[{**row, 'display_name':custom}]}))
                account = q.load_accounts(config, 'both')[0]
                self.assertEqual(getattr(account, 'display_name', None), expected)

    def test_existing_account_can_set_custom_display_name(self):
        config = {'accounts':[{'name':'global', 'region':'global'}]}
        with patch('builtins.input', side_effect=['n','global','我的主账号','']):
            cli.wizard_accounts(config, Path('/tmp'))
        self.assertEqual(config['accounts'][0]['display_name'], '我的主账号')
