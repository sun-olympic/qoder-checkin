import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import checkin_cli as cli
import qoder_checkin as q
from qoder_schedule import Scheduler, ScheduleError


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root/'config.json'
        self.config.write_text(json.dumps({'accounts':[{'name':'a','region':'global','token_file':'token.json'}]}))
        (self.root/'token.json').write_text('{"token":"fake"}')

    def test_configs_with_same_stem_have_independent_locks(self):
        with cli.run_lock(self.config) as first:
            with cli.run_lock(self.root/'config.yaml') as second:
                self.assertTrue(first)
                self.assertTrue(second)

    def test_mutating_commands_refuse_concurrent_management(self):
        original = self.config.read_bytes()
        with cli.run_lock(self.config.with_name(self.config.name+'.setup')) as held:
            self.assertTrue(held)
            with patch.object(Scheduler,'install') as install, patch.object(Scheduler,'uninstall') as uninstall, \
                    patch('builtins.input') as prompt, contextlib.redirect_stderr(io.StringIO()):
                for command in ('install','uninstall','wizard'):
                    self.assertEqual(cli.main([command,'--config',str(self.config)]),2)
            install.assert_not_called();uninstall.assert_not_called();prompt.assert_not_called()
        self.assertEqual(self.config.read_bytes(),original)
        with patch.object(Scheduler,'install'),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['install','--config',str(self.config)]),0)

    def test_cancelled_identity_write_leaves_no_partial_identity(self):
        scheduler = Scheduler(self.config)
        with patch('qoder_schedule.os.fsync',side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):scheduler.preserve_identity()
        self.assertFalse(scheduler.identity_file.exists())
        self.assertFalse(list(self.root.glob('.config.json.schedule-id-*')))
        scheduler.preserve_identity()
        self.assertEqual(Scheduler(self.config).key,scheduler.key)

    def test_identity_link_failure_preserves_existing_identity(self):
        scheduler = Scheduler(self.config)
        def competitor(source,target):
            Path(target).write_text('123456789abc\n')
            raise FileExistsError()
        with patch('qoder_schedule.os.link',side_effect=competitor):
            with self.assertRaises(ScheduleError):
                scheduler.preserve_identity()
        self.assertEqual(scheduler.identity_file.read_text(),'123456789abc\n')

    def test_extreme_timestamps_are_api_errors(self):
        campaign = {'campaignId':'daily','claimStatus':'CLAIMABLE','benefit':{'amount':100},'startAt':0,'endAt':200}
        for start,end in [(10**400,10**401),(-1,200),(0,float('inf')),(0,float('nan'))]:
            with self.subTest(start=str(start)[:20],end=str(end)[:20]):
                with self.assertRaises(q.ApiError):
                    q.CampaignClient.summarize({**campaign,'startAt':start,'endAt':end})

    def test_bad_timestamp_does_not_prevent_second_account(self):
        data=json.loads(self.config.read_text())
        data['accounts'].append({'name':'b','region':'global','token_file':'token.json'})
        self.config.write_text(json.dumps(data))
        campaign={'campaignId':'daily','claimStatus':'CLAIMABLE','actionType':'CLAIM_BENEFIT',
                  'benefit':{'kind':'CREDITS','amount':100},'startAt':10**400,'endAt':10**401,
                  'placements':[{'content':{'en':{'title':'Daily Credits'}}}]}
        good={**campaign,'startAt':0,'endAt':253402300799,'claimStatus':'CLAIMED'}
        with patch.object(q.Transport,'send',side_effect=[q.Response(200,{'campaigns':[campaign]}),q.Response(200,{'campaigns':[good]})]):
            results=q.run_once(self.config,'both','claim',20)
        self.assertEqual([r['result'] for r in results],['error','already_claimed'])


if __name__=='__main__':unittest.main()
