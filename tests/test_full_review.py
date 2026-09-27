import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import qoder_checkin as q


class Reply(io.BytesIO):
    def __init__(self, raw=b'{}', status=200, headers=None):
        super().__init__(raw)
        self.code = status
        self.headers = headers or {}


class FullReviewTests(unittest.TestCase):
    def test_legacy_binding_rejects_wrong_or_missing_server_uid(self):
        for uid in ('other', None):
            transport = Mock()
            transport.send.return_value = q.Response(200, {'uid':uid,'status':'CLAIMABLE'})
            with self.assertRaises(q.AuthError):q.Client(transport,'owner').claim()
            self.assertEqual([c.args[0] for c in transport.send.call_args_list],['GET'])

    def test_legacy_binding_allows_verified_owner(self):
        transport = Mock()
        transport.send.side_effect = [q.Response(200,{'uid':'owner','status':'CLAIMABLE'}),q.Response(200,{'success':True})]
        self.assertEqual(q.Client(transport,'owner').claim()['result'],'claimed')

    def test_run_once_applies_binding_to_legacy_environment_token(self):
        with tempfile.TemporaryDirectory() as d:
            config = Path(d)/'config.json'
            config.write_text(json.dumps({'accounts':[{'name':'a','region':'global','api_mode':'legacy',
                'expected_account_id':'owner','token_env':'REVIEW_TOKEN'}]}))
            with patch.dict(os.environ,{'REVIEW_TOKEN':'fake'}), \
                    patch.object(q.Transport,'send',return_value=q.Response(200,{'status':'CLAIMABLE','uid':'other'})) as send:
                result=q.run_once(config,'global','claim',20)
            self.assertEqual(result[0]['result'],'error')
            self.assertEqual(send.call_count,1)

    def test_huge_retry_after_is_capped_without_integer_conversion_error(self):
        for header,delay in [('9'*5000,30),('0'*5000+'5',5),('²',1)]:
            transport = q.Transport(q.BASES['global'],'fake')
            with patch.object(transport.opener,'open',side_effect=[Reply(status=429,headers={'Retry-After':header}),Reply()]), \
                    patch.object(q.time,'sleep') as sleep:
                self.assertEqual(transport.send('GET','/test').status,200)
                sleep.assert_called_once_with(delay)

    def test_deep_response_json_is_reported_as_api_error(self):
        raw=b'{"data":'+b'['*2000+b'0'+b']'*2000+b'}'
        transport=q.Transport(q.BASES['global'],'fake')
        with patch.object(transport.opener,'open',return_value=Reply(raw)):
            with self.assertRaises(q.ApiError):q.payload(transport.send('GET','/test'))
        # JSON parser depth limits differ across supported Python versions.
        with patch.object(transport.opener,'open',return_value=Reply(b'{}')), \
                patch.object(q.json,'loads',side_effect=RecursionError()):
            with self.assertRaises(q.ApiError):transport.send('GET','/test')

    def test_deep_config_and_credentials_are_config_errors(self):
        raw='{"data":'+'['*2000+'0'+']'*2000+'}'
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'file.json';file.write_text(raw)
            with self.assertRaises(q.ConfigError):q.load_accounts(file,'both')
            with self.assertRaises(q.ConfigError):q.Account('a','global',q.BASES['global'],'',file).token()

    def test_client_version_requires_ascii_header_characters(self):
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'config.json'
            for version in ('版本1','0.4.2\r\nx:bad'):
                file.write_text(json.dumps({'accounts':[{'region':'global','client_version':version}]}))
                with self.assertRaises(q.ConfigError):q.load_accounts(file,'both')

    @unittest.skipUnless(shutil.which('node'),'Node.js not installed')
    def test_importer_rejects_invalid_arguments_before_reading_auth(self):
        cases=[[],['global'],['--region'],['--region','global','--output'],
               ['--region','global','--region','cn'],['--region','__proto__'],
               ['--region','global','--unknown','x']]
        for args in cases:
            with self.subTest(args=args):
                result=subprocess.run([shutil.which('node'),str(q.ROOT/'import_macos_auth.mjs'),*args],capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,2)
                self.assertEqual(json.loads(result.stdout)['code'],'INVALID_ARGUMENTS')


if __name__=='__main__':unittest.main()
