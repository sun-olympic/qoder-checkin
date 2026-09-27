import json
import plistlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

import qoder_checkin as q
import qoder_device as device


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.app=self.root/'Qoder.app';(self.app/'Contents').mkdir(parents=True)
        (self.app/'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleShortVersionString':'0.4.2'}))
        identity=self.root/'Library/Application Support/com.qoder.app.stable/auth.machine-id'
        identity.parent.mkdir(parents=True)
        identity.write_text('12345678-1234-4234-8234-123456789abc')
        self.output=json.dumps({'machineToken':'fake-device-token','machineCode':'fake-code','machineType':'fake-type'})

    def native(self, result):
        with patch.object(device.sys,'platform','darwin'),patch.object(device.Path,'home',return_value=self.root), \
                patch.object(device.subprocess,'run',return_value=result) as run:
            headers=device.native_headers('global','owner',str(self.app))
        args=run.call_args.args[0]
        self.assertEqual(args,[str(self.app/'Contents/Resources/umid/runtime-info'),'3','--account-stdin'])
        self.assertEqual(json.loads(run.call_args.kwargs['input']),{'account':'owner'})
        return headers

    def test_uses_vendor_cli_without_launching_ide(self):
        headers=self.native(subprocess.CompletedProcess([],0,self.output,''))
        self.assertEqual(headers['Cosy-MachineToken'],'fake-device-token')
        self.assertEqual(headers['Cosy-Version'],'0.4.2')

    def test_invalid_vendor_output_is_not_printed(self):
        with self.assertRaises(device.DeviceError) as caught:
            self.native(subprocess.CompletedProcess([],0,'secret-invalid-json',''))
        self.assertNotIn('secret',str(caught.exception))

    def test_cannot_override_auth_or_inject_headers(self):
        for headers in ({'Authorization':'other'},{'Cosy-MachineToken':'x\r\nHost: evil'}, {'Cosy-MachineId':None}):
            with self.assertRaises(q.ConfigError):q.Transport(q.BASES['global'],'fake',device_headers=headers)

    def test_device_headers_attached_to_get_and_post(self):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=None)
        response.code=200;response.headers={};response.read.return_value=b'{}'
        t=q.Transport(q.BASES['global'],'fake',device_headers={'Cosy-MachineToken':'device-fake'})
        with patch.object(t.opener,'open',return_value=response) as send:
            for method in ('GET','POST'):t.send(method,'/test',empty_body=True)
        for call in send.call_args_list:
            self.assertEqual(call.args[0].get_header('Cosy-machinetoken'),'device-fake')
            self.assertEqual(call.args[0].get_header('Authorization'),'Bearer fake')

    def test_device_mode_requires_bound_account(self):
        account=q.Account('a','global',q.BASES['global'],'TOKEN',device_source='qoder')
        with self.assertRaises(q.ConfigError):account.device_headers()

    def test_helper_failure_stops_request_without_fallback(self):
        config=self.root/'config.json'
        config.write_text(json.dumps({'accounts':[{'name':'a','region':'global','token_file':'token.json',
            'device_source':'qoder','expected_account_id':'owner'}]}))
        (self.root/'token.json').write_text('{"token":"fake"}')
        with patch.object(q,'native_headers',side_effect=device.DeviceError('helper failed')),patch.object(q.Transport,'send') as send:
            results=q.run_once(config,'global','claim',20)
        self.assertEqual(results[0]['result'],'error');send.assert_not_called()


if __name__=='__main__':unittest.main()
