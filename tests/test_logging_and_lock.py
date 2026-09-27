import contextlib
import errno
import io
import json
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import checkin_cli as cli


class WindowsLockTests(unittest.TestCase):
    def exercise(self, failure=None):
        file = Mock()
        file.read.side_effect = AssertionError('must not read locked bytes')
        crt = SimpleNamespace(LK_NBLCK=2, locking=Mock(side_effect=failure))
        stub = SimpleNamespace(name='nt', open=Mock(return_value=123),
                               fdopen=Mock(return_value=file), O_RDWR=2, O_CREAT=512)
        with patch.object(cli, 'os', stub), patch.dict('sys.modules', {'msvcrt': crt}):
            try:
                with cli.run_lock(Path('/test/config.json')) as acquired:
                    return acquired
            finally:
                file.close.assert_called_once()
                file.read.assert_not_called()
                file.write.assert_not_called()

    def test_empty_file_can_be_locked_without_read_or_write(self):
        self.assertTrue(self.exercise())

    def test_contention_skips_instead_of_raising(self):
        self.assertFalse(self.exercise(OSError(errno.EACCES, 'locked')))
        error = OSError(errno.EIO, 'locked')
        error.winerror = 33
        self.assertFalse(self.exercise(error))

    def test_other_io_errors_are_not_mistaken_for_contention(self):
        with self.assertRaises(OSError): self.exercise(OSError(errno.EBADF, 'invalid handle'))
        error = OSError(errno.EACCES, 'access denied')
        error.winerror = 5
        with self.assertRaises(OSError): self.exercise(error)


class LogFailoverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root/'config.json'
        self.config.write_text('{"accounts":[{"name":"a","region":"global"}]}')
        self.home = self.root/'home'
        self.result = [{'account':'a','region':'global','result':'already_claimed','message':'ok'}]

    def run_cli(self):
        with patch('checkin_cli.Path.home',return_value=self.home), \
                patch('checkin_cli.q.run_once',return_value=self.result) as action, \
                contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(['run','--config',str(self.config)])
        action.assert_called_once()
        return rc

    def test_rotation_failure_replays_record_in_fallback(self):
        (self.root/'checkin.log').write_text('x'*1048576)
        with patch('logging.handlers.os.rename',side_effect=PermissionError('SECRET')):
            self.assertEqual(self.run_cli(),0)
        logs=list((self.home/'.qoder-checkin/logs').glob('*.log'))
        self.assertEqual(len(logs),1)
        data=json.loads(logs[0].read_text())
        self.assertEqual(data['accounts'],self.result)
        self.assertNotIn('SECRET',logs[0].read_text())

    def test_write_failure_replays_record_in_fallback(self):
        original = cli.StrictRotatingFileHandler.emit
        def fail_primary(handler,record):
            if handler.baseFilename == str((self.root/'checkin.log').resolve()):
                raise OSError('disk full')
            return original(handler,record)
        with patch.object(cli.StrictRotatingFileHandler,'emit',fail_primary):
            self.assertEqual(self.run_cli(),0)
        logs=list((self.home/'.qoder-checkin/logs').glob('*.log'))
        self.assertEqual(json.loads(logs[0].read_text())['accounts'],self.result)

    def test_both_destinations_failing_returns_nonzero_without_rerunning_api(self):
        with patch.object(cli.StrictRotatingFileHandler,'emit',side_effect=OSError('disk full')):
            self.assertEqual(self.run_cli(),2)

    def test_successful_rotation_keeps_private_permissions(self):
        (self.root/'checkin.log').write_text('x'*1048576)
        self.assertEqual(self.run_cli(),0)
        self.assertTrue((self.root/'checkin.log.1').exists())
        self.assertEqual(json.loads((self.root/'checkin.log').read_text())['accounts'],self.result)
        if os.name!='nt':
            self.assertEqual((self.root/'checkin.log').stat().st_mode & 0o777,0o600)
            self.assertEqual((self.root/'checkin.log.1').stat().st_mode & 0o777,0o600)


if __name__=='__main__': unittest.main()
