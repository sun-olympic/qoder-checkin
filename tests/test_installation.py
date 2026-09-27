import json
import contextlib
import io
import shutil
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import checkin_cli as cli
import qoder_checkin as q
from qoder_schedule import Scheduler, ScheduleError


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'folder with spaces & 中文'
        self.root.mkdir()
        self.config=self.root/'config.json'
        self.config.write_text(json.dumps({'accounts':[{'name':'global','region':'global',
            'token_file':'credentials.json'}]}))
        self.cred=self.root/'credentials.json'
        self.cred.write_text(json.dumps({'token':'test-token'}))

    def test_macos_paths_and_login_trigger(self):
        s=Scheduler(self.config,at='11:23',platform='darwin')
        data=plistlib.loads(s.definition())
        self.assertEqual(data['ProgramArguments'][-3:], [str(self.config),'--region','both'])
        self.assertTrue(data['RunAtLoad'])
        self.assertEqual(data['StartCalendarInterval'],{'Hour':11,'Minute':23})
        self.assertNotIn('test-token',s.definition().decode())

    def test_windows_xml_single_task_and_catchup(self):
        s=Scheduler(self.config,platform='win32')
        with patch.object(s,'command',return_value=subprocess.CompletedProcess([],0,'PC\\user\n','')):
            root=ET.fromstring(s.definition())
        ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(len(root.find('t:Triggers',ns)),2)
        self.assertEqual(root.find('.//t:LogonType',ns).text,'InteractiveToken')
        self.assertEqual(root.find('.//t:StartWhenAvailable',ns).text,'true')
        self.assertIn('"'+str(self.config)+'"',root.find('.//t:Arguments',ns).text)
        self.assertEqual(root.find('.//t:MultipleInstancesPolicy',ns).text,'IgnoreNew')

    def test_install_failure_restores_previous_macos_definition(self):
        s=Scheduler(self.config,platform='darwin'); s.plist=self.root/'agent.plist'
        s.plist.write_bytes(b'previous')
        def command(args):
            return subprocess.CompletedProcess(args,1 if args[1]=='bootstrap' and s.plist.read_bytes()!=b'previous' else 0,'','')
        with patch.object(s,'command',side_effect=command), patch('qoder_schedule.os.getuid',return_value=501,create=True):
            with self.assertRaises(ScheduleError): s.install()
        self.assertEqual(s.plist.read_bytes(),b'previous')

    def test_install_failure_preserves_windows_xml_until_command_done(self):
        s=Scheduler(self.config,platform='win32')
        seen=[]
        def command(args):
            if args[0]=='whoami': return subprocess.CompletedProcess(args,0,'PC\\user','')
            p=Path(args[-1]); self.assertTrue(p.exists()); ET.fromstring(p.read_bytes()); seen.append(p)
            return subprocess.CompletedProcess(args,1,'','denied')
        with patch.object(s,'command',side_effect=command), patch.object(s,'status',return_value=False):
            with self.assertRaises(ScheduleError): s.install()
        self.assertFalse(seen[0].exists())

    def test_lock_excludes_second_process_and_recovers(self):
        command=[sys.executable,'-c',
            'import checkin_cli as c; from pathlib import Path; import sys;\n'
            'with c.run_lock(Path(sys.argv[1])) as ok: print(ok)',str(self.config)]
        with cli.run_lock(self.config) as acquired:
            self.assertTrue(acquired)
            out=subprocess.check_output(command,text=True,cwd=q.ROOT)
            self.assertEqual(out.strip(),'False')
        with cli.run_lock(self.config) as acquired: self.assertTrue(acquired)

    def test_expired_file_rejected_before_network(self):
        self.cred.write_text(json.dumps({'token':'secret','expiresAt':'2020-01-01T00:00:00Z'}))
        account=q.load_accounts(self.config,'global')[0]
        with self.assertRaises(q.AuthError): account.token()

    def test_wrong_file_account_rejected(self):
        self.cred.write_text(json.dumps({'token':'secret','account_id':'other'}))
        account=q.load_accounts(self.config,'global')[0]; account.expected_account_id='expected'
        with self.assertRaises(q.AuthError): account.token()

    def test_doctor_does_not_sync_or_network(self):
        with patch.object(q.Account,'sync_login') as sync, patch.object(q.Transport,'send') as send:
            self.assertEqual(cli.main(['doctor','--config',str(self.config)]),0)
        sync.assert_not_called(); send.assert_not_called()

    def test_installer_rejects_background_keychain_prompt(self):
        data=json.loads(self.config.read_text()); data['accounts'][0]['auth_source']='macos'
        cli.save_config(self.config,data)
        with patch('checkin_cli.doctor',return_value=0),patch.object(Scheduler,'install') as install:
            self.assertEqual(cli.main(['install','--config',str(self.config)]),2)
        install.assert_not_called()

    def test_wizard_keeps_existing_accounts_and_credentials(self):
        original=self.cred.read_bytes()
        with patch('builtins.input',side_effect=['11:00', '1']):
            self.assertEqual(cli.main(['wizard','--config',str(self.config)]),0)
        self.assertEqual(self.cred.read_bytes(),original)
        data=json.loads(self.config.read_text())
        self.assertEqual(data['accounts'][0]['name'],'global')
        self.assertEqual(data['schedule']['at'],'11:00')
        if os.name!='nt': self.assertEqual(self.config.stat().st_mode & 0o777,0o600)

    def test_linux_install_unsupported_without_mutation(self):
        s=Scheduler(self.config,platform='linux')
        with patch.object(s,'command') as command:
            with self.assertRaises(ScheduleError): s.install()
        command.assert_not_called()

    def test_time_validation(self):
        for bad in ['24:00','10:60','9:00',[],None]:
            with self.assertRaises(q.ConfigError): cli.validate_time(bad)

    def test_preparation_failure_keeps_old_service_running(self):
        s=Scheduler(self.config,platform='darwin');s.plist=self.root/'agent.plist'
        s.plist.write_bytes(b'previous');s.preserve_identity()
        with patch.object(s,'status',return_value=True), patch.object(s,'command') as command, \
                patch('qoder_schedule.os.open',side_effect=PermissionError('test')):
            with self.assertRaises(PermissionError): s.install()
        command.assert_not_called()
        self.assertEqual(s.plist.read_bytes(),b'previous')

    def test_replace_failure_reloads_old_service(self):
        s=Scheduler(self.config,platform='darwin');s.plist=self.root/'agent.plist'
        s.plist.write_bytes(b'previous')
        active=True; commands=[]
        def command(args):
            nonlocal active
            commands.append(args[1])
            active=args[1]=='bootstrap'
            return subprocess.CompletedProcess(args,0,'','')
        replace=os.replace
        attempts=0
        def fail_once(source,target):
            nonlocal attempts
            attempts+=1
            if attempts==1: raise PermissionError('test')
            return replace(source,target)
        with patch.object(s,'status',side_effect=lambda:active), patch.object(s,'command',side_effect=command), \
                patch('qoder_schedule.os.replace',side_effect=fail_once), \
                patch('qoder_schedule.os.getuid',return_value=501,create=True):
            with self.assertRaisesRegex(ScheduleError,'已恢复'): s.install()
        self.assertTrue(active)
        self.assertEqual(commands,['bootout','bootstrap'])
        self.assertEqual(s.plist.read_bytes(),b'previous')

    def test_bootstrap_exception_restores_old_service(self):
        s=Scheduler(self.config,platform='darwin');s.plist=self.root/'agent.plist'
        s.plist.write_bytes(b'previous');active=True
        def command(args):
            nonlocal active
            if args[1]=='bootstrap' and s.plist.read_bytes()!=b'previous':
                raise ScheduleError('timeout')
            active=args[1]=='bootstrap'
            return subprocess.CompletedProcess(args,0,'','')
        with patch.object(s,'status',side_effect=lambda:active),patch.object(s,'command',side_effect=command), \
                patch('qoder_schedule.os.getuid',return_value=501,create=True):
            with self.assertRaisesRegex(ScheduleError,'已恢复'):s.install()
        self.assertTrue(active)
        self.assertEqual(s.plist.read_bytes(),b'previous')

    def test_rollback_failure_is_not_reported_as_restored(self):
        s=Scheduler(self.config,platform='darwin');s.plist=self.root/'agent.plist'
        s.plist.write_bytes(b'previous')
        with patch.object(s,'status',return_value=True), \
                patch.object(s,'command',side_effect=lambda args:subprocess.CompletedProcess(args,1 if args[1]=='bootstrap' else 0,'','')), \
                patch('qoder_schedule.os.getuid',return_value=501,create=True):
            with self.assertRaisesRegex(ScheduleError,'回滚未完成'):s.install()

    def test_uninstall_ignores_missing_invalid_and_bad_time_config(self):
        for content in [None,'{bad',json.dumps({'schedule':{'at':'invalid'}})]:
            if content is None:self.config.unlink(missing_ok=True)
            else:self.config.write_text(content)
            with patch.object(Scheduler,'uninstall') as uninstall:
                self.assertEqual(cli.main(['uninstall','--config',str(self.config)]),0)
            uninstall.assert_called_once()

    def test_sidecar_preserves_legacy_identity_after_move(self):
        old=Scheduler(self.config);old.preserve_identity()
        key=old.key
        moved=self.root.parent/'moved'
        shutil.move(str(self.root),moved)
        new=Scheduler(moved/'config.json')
        self.assertEqual(new.label,old.label)
        self.assertEqual(new.task,old.task)
        self.assertEqual(new.key,key)
        self.assertIn(str(moved/'config.json'),new.argv)
        (moved/'config.json').unlink()
        self.assertEqual(Scheduler(moved/'config.json').label,old.label)

    def test_sidecar_not_replaced_during_repeat_install(self):
        s=Scheduler(self.config);s.preserve_identity()
        content=s.identity_file.read_bytes()
        s.preserve_identity()
        self.assertEqual(s.identity_file.read_bytes(),content)

    def test_pythonw_startup_failure_is_persisted(self):
        self.config.write_text('{invalid-json')
        with open(os.devnull,'w') as sink,patch.object(sys,'stdout',sink),patch.object(sys,'stderr',sink):
            self.assertEqual(cli.main(['run','--config',str(self.config)]),2)
        record=json.loads((self.root/'checkin.log').read_text())
        self.assertEqual(record['error_type'],'ConfigError')
        self.assertNotIn('invalid-json',record['message'])

    def test_lock_failure_is_persisted_without_exception_secrets(self):
        with patch('checkin_cli.run_lock',side_effect=PermissionError('SECRET_TOKEN')),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['run','--config',str(self.config)]),2)
        log=(self.root/'checkin.log').read_text()
        self.assertIn('PermissionError',log)
        self.assertNotIn('SECRET_TOKEN',log)

    def test_log_falls_back_when_project_is_unwritable(self):
        self.config.write_text('{invalid')
        real_open=os.open
        def selective_open(path,*args,**kwargs):
            if Path(path)==self.root/'checkin.log':raise PermissionError('denied')
            return real_open(path,*args,**kwargs)
        home=self.root.parent/'home'
        with patch('checkin_cli.os.open',side_effect=selective_open),patch('checkin_cli.Path.home',return_value=home), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['run','--config',str(self.config)]),2)
        logs=list((home/'.qoder-checkin/logs').glob('*.log'))
        self.assertEqual(len(logs),1)
        self.assertEqual(json.loads(logs[0].read_text())['result'],'error')

    def test_query_permission_failure_does_not_delete_or_report_missing(self):
        for platform in ('darwin','win32'):
            s=Scheduler(self.config,platform=platform);s.plist=self.root/'old.plist'
            s.plist.write_bytes(b'old')
            with patch.object(s,'command',return_value=subprocess.CompletedProcess([],1,'','Access is denied.')) as command, \
                    patch('qoder_schedule.os.getuid',return_value=501,create=True):
                with self.assertRaisesRegex(ScheduleError,'无法查询'):s.uninstall()
            self.assertEqual(command.call_count,1)
            self.assertTrue(s.plist.exists())

    def test_only_explicit_missing_result_means_unregistered(self):
        for platform,message in [('darwin','Could not find service "example" in domain'),
                                 ('win32','ERROR: The system cannot find the file specified.'),
                                 ('win32','错误: 系统找不到指定的文件。')]:
            s=Scheduler(self.config,platform=platform)
            with patch.object(s,'command',return_value=subprocess.CompletedProcess([],1,'',message)), \
                    patch('qoder_schedule.os.getuid',return_value=501,create=True):
                self.assertFalse(s.status())

    def test_config_save_failure_does_not_install_native_task(self):
        original=self.config.read_bytes()
        with patch('checkin_cli.save_config',side_effect=PermissionError('disk')), \
                patch.object(Scheduler,'install') as install,contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['install','--config',str(self.config)]),2)
        install.assert_not_called()
        self.assertEqual(self.config.read_bytes(),original)

    def test_native_install_failure_restores_exact_config(self):
        original=self.config.read_bytes()
        def fail(scheduler):
            self.assertEqual(json.loads(self.config.read_text())['schedule']['at'],'11:00')
            raise ScheduleError('installation rolled back')
        with patch.object(Scheduler,'install',autospec=True,side_effect=fail),contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['install','--config',str(self.config),'--at','11:00']),2)
        self.assertEqual(self.config.read_bytes(),original)

    def test_time_only_reinstall_keeps_region_and_explicit_override_wins(self):
        config=json.loads(self.config.read_text());config['schedule']={'at':'10:05','region':'global'}
        cli.save_config(self.config,config)
        with patch.object(Scheduler,'install',autospec=True) as install:
            self.assertEqual(cli.main(['install','--config',str(self.config),'--at','11:00']),0)
            self.assertEqual(install.call_args.args[0].region,'global')
            self.assertEqual(cli.main(['install','--config',str(self.config),'--region','both']),0)
            self.assertEqual(install.call_args.args[0].region,'both')
        self.assertEqual(json.loads(self.config.read_text())['schedule']['region'],'both')

    def test_wizard_time_edit_preserves_schedule_region(self):
        config=json.loads(self.config.read_text());config['schedule']={'at':'10:05','region':'global'}
        cli.save_config(self.config,config)
        with patch('builtins.input',side_effect=['11:00', '1']):
            self.assertEqual(cli.main(['wizard','--config',str(self.config)]),0)
        self.assertEqual(json.loads(self.config.read_text())['schedule']['region'],'global')

    def test_windows_failed_verification_restores_previous_definition(self):
        s=Scheduler(self.config,platform='win32')
        old='<Task><Old /></Task>';registered=[]
        def command(args):
            if args[0]=='whoami':return subprocess.CompletedProcess(args,0,'PC\\user','')
            return subprocess.CompletedProcess(args,0,old,'')
        with patch.object(s,'command',side_effect=command), \
                patch.object(s,'status',side_effect=[True,False,True]), \
                patch.object(s,'register_windows',side_effect=lambda value:registered.append(value)):
            with self.assertRaisesRegex(ScheduleError,'已恢复'):s.install()
        self.assertEqual(len(registered),2)
        self.assertIsNotNone(ET.fromstring(registered[1]).find('Old'))

    def test_windows_new_task_failed_verification_is_removed(self):
        s=Scheduler(self.config,platform='win32')
        with patch.object(s,'definition',return_value=b'xml'), \
                patch.object(s,'register_windows'), \
                patch.object(s,'status',side_effect=[False,False,True]), \
                patch.object(s,'command',return_value=subprocess.CompletedProcess([],0,'','')) as command:
            with self.assertRaisesRegex(ScheduleError,'已恢复'):s.install()
        self.assertIn('/Delete',command.call_args.args[0])


if __name__=='__main__': unittest.main()
