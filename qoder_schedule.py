"""Native per-user scheduling. Definitions contain paths, never credentials."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime


class ScheduleError(Exception):
    pass


class Scheduler:
    def __init__(self, config: Path, region='both', at='10:05', platform=None):
        self.config = config.resolve()
        self.region, self.at = region, at
        self.platform = platform or sys.platform
        key = hashlib.sha256(str(self.config).encode()).hexdigest()[:12]
        self.identity_file = self.config.with_name(self.config.name + '.schedule-id')
        if self.identity_file.exists():
            key = self.identity_file.read_text(encoding='ascii').strip()
            if not re.fullmatch(r'[0-9a-f]{12}', key):
                raise ScheduleError('任务标识文件损坏，请恢复 .schedule-id 文件')
        self.key = key
        self.label = 'com.qoder.checkin.' + key
        self.task = 'QoderCheckin-' + key
        self.plist = Path.home() / 'Library/LaunchAgents' / (self.label + '.plist')
        self.argv = [sys.executable, str(Path(__file__).with_name('checkin_cli.py')),
                     'run', '--config', str(self.config), '--region', region]

    def command(self, args):
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=40)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ScheduleError('无法执行系统任务管理器：' + type(exc).__name__) from None

    def supported(self):
        if self.platform not in ('darwin', 'win32'):
            raise ScheduleError('原生安装支持 macOS / Windows；其他平台请使用 daemon 或 CI')

    def status(self):
        self.supported()
        args = (['launchctl', 'print', f'gui/{os.getuid()}/{self.label}']
                if self.platform == 'darwin' else ['schtasks', '/Query', '/TN', self.task, '/XML'])
        result = self.command(args)
        if result.returncode == 0:
            return True
        detail = (result.stdout + '\n' + result.stderr).lower()
        missing = (('could not find service', 'could not find specified service')
                   if self.platform == 'darwin' else
                   ('the system cannot find the file specified', '系统找不到指定的文件'))
        if any(message in detail for message in missing):
            return False
        raise ScheduleError('无法查询任务状态；请检查系统任务权限，未将查询失败视为未注册')

    def runtime_status(self):
        """Only expose selected operational fields, not launchd's environment."""
        if self.platform != 'darwin':
            return '请在任务计划程序查看上次运行结果；领取结果见 checkin.log。'
        result = self.command(['launchctl', 'print', f'gui/{os.getuid()}/{self.label}'])
        if result.returncode:
            return '无法读取任务运行状态。'
        fields = []
        for line in result.stdout.splitlines():
            # Main properties are one-tab indented; nested states are irrelevant.
            if line.startswith('\t') and not line.startswith('\t\t'):
                item = line.strip()
                if item.startswith(('state =', 'runs =', 'pid =', 'last exit code =')):
                    fields.append(item)
        return '; '.join(fields) or '尚无运行记录。'

    def definition(self):
        hour, minute = map(int, self.at.split(':'))
        if self.platform == 'darwin':
            return plistlib.dumps({'Label': self.label, 'ProgramArguments': self.argv,
                'WorkingDirectory': str(self.config.parent), 'RunAtLoad': True,
                'StartCalendarInterval': {'Hour': hour, 'Minute': minute},
                'ProcessType': 'Background',
                'EnvironmentVariables': {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
                                         'PYTHONIOENCODING': 'utf-8'},
                'StandardOutPath': str(self.config.parent / 'scheduler.log'),
                'StandardErrorPath': str(self.config.parent / 'scheduler.log')})
        # One task owns both triggers, avoiding partial installation of a pair.
        ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
        ET.register_namespace('', ns)
        def child(parent, name, value=None, **attrs):
            el = ET.SubElement(parent, '{'+ns+'}'+name, attrs)
            if value is not None: el.text = str(value)
            return el
        task = ET.Element('{'+ns+'}Task', {'version': '1.2'})
        triggers = child(task, 'Triggers')
        daily = child(triggers, 'CalendarTrigger')
        child(daily, 'StartBoundary', f'{datetime.now():%Y-%m-%d}T{self.at}:00')
        child(daily, 'Enabled', 'true')
        child(child(daily, 'ScheduleByDay'), 'DaysInterval', '1')
        username = self.command(['whoami']).stdout.strip()
        if not username: raise ScheduleError('无法确定 Windows 当前用户')
        login = child(triggers, 'LogonTrigger')
        child(login, 'Enabled', 'true'); child(login, 'UserId', username)
        principal = child(child(task, 'Principals'), 'Principal', id='Author')
        child(principal, 'UserId', username)
        child(principal, 'LogonType', 'InteractiveToken')
        child(principal, 'RunLevel', 'LeastPrivilege')
        settings = child(task, 'Settings')
        for name, value in [('MultipleInstancesPolicy','IgnoreNew'),
                            ('DisallowStartIfOnBatteries','false'), ('StopIfGoingOnBatteries','false'),
                            ('StartWhenAvailable','true'), ('ExecutionTimeLimit','PT30M')]:
            child(settings, name, value)
        action = child(child(task, 'Actions', Context='Author'), 'Exec')
        python = Path(self.argv[0]).with_name('pythonw.exe')
        child(action, 'Command', str(python) if python.exists() else self.argv[0])
        child(action, 'Arguments', subprocess.list2cmdline(self.argv[1:]))
        child(action, 'WorkingDirectory', str(self.config.parent))
        return ET.tostring(task, encoding='utf-16', xml_declaration=True)

    def preserve_identity(self):
        # Seed with the legacy path hash so existing registrations keep their name.
        # Move this sidecar with the project; business config may be missing/broken.
        if self.identity_file.exists():
            if self.identity_file.read_text(encoding='ascii').strip() != self.key:
                raise ScheduleError('任务标识已改变，请重新执行命令')
            return
        staged = self.stage_file(self.identity_file, (self.key + '\n').encode('ascii'))
        try:
            # Publish fully written content without replacing another install's ID.
            try:
                os.link(staged, self.identity_file)
            except FileExistsError:
                if self.identity_file.read_text(encoding='ascii').strip() != self.key:
                    raise ScheduleError('任务标识已改变，请重新执行命令') from None
        finally:
            staged.unlink(missing_ok=True)

    @staticmethod
    def stage_file(path, content):
        fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '-')
        staged = Path(name)
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        return staged

    def install(self):
        self.supported()
        definition = self.definition()
        self.preserve_identity()
        if self.platform == 'darwin':
            self.plist.parent.mkdir(parents=True, exist_ok=True)
            previous = self.plist.read_bytes() if self.plist.exists() else None
            loaded = self.status()
            if loaded and previous is None:
                raise ScheduleError('旧任务缺少定义文件，无法保证回滚，未修改任务')
            # Finish all preparatory writes before stopping the working service.
            log = self.config.parent / 'scheduler.log'
            fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
            log.chmod(0o600)
            staged = self.stage_file(self.plist, definition)
            backup = None
            keep_backup = False
            try:
                if previous is not None:
                    backup = self.stage_file(self.plist, previous)
                changed = False
                try:
                    if loaded:
                        changed = True
                        result = self.command(['launchctl', 'bootout', f'gui/{os.getuid()}/{self.label}'])
                        if result.returncode:
                            changed = False
                            raise ScheduleError('无法卸载旧任务，未覆盖安装')
                    changed = True
                    os.replace(staged, self.plist)
                    result = self.command(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(self.plist)])
                    if result.returncode or not self.status():
                        raise ScheduleError('launchd 注册或验证失败')
                except (OSError, ScheduleError, KeyboardInterrupt) as failure:
                    if not changed:
                        raise
                    try:
                        if self.status():
                            result = self.command(['launchctl', 'bootout', f'gui/{os.getuid()}/{self.label}'])
                            if result.returncode:
                                raise ScheduleError('无法停止新任务')
                        if backup is not None:
                            os.replace(backup, self.plist)
                        else:
                            self.plist.unlink(missing_ok=True)
                        if loaded:
                            result = self.command(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(self.plist)])
                            if result.returncode or not self.status():
                                raise ScheduleError('旧任务无法重新加载')
                    except (OSError, ScheduleError, KeyboardInterrupt):
                        keep_backup = True
                        recovery = backup if backup is not None and backup.exists() else self.plist
                        raise ScheduleError(f'安装失败且回滚未完成；恢复文件：{recovery}，请检查任务状态') from failure
                    if isinstance(failure, KeyboardInterrupt):
                        raise
                    raise ScheduleError('安装失败，已恢复安装前的任务状态') from failure
            finally:
                staged.unlink(missing_ok=True)
                if backup is not None and not keep_backup:
                    backup.unlink(missing_ok=True)
        else:
            previous = None
            if self.status():
                snapshot = self.command(['schtasks', '/Query', '/TN', self.task, '/XML'])
                if snapshot.returncode:
                    raise ScheduleError('无法备份旧任务，未修改安装')
                try:
                    previous = ET.tostring(ET.fromstring(snapshot.stdout), encoding='utf-16',
                                           xml_declaration=True)
                except ET.ParseError:
                    raise ScheduleError('旧任务 XML 无效，未修改安装') from None
            try:
                self.register_windows(definition)
                if not self.status():
                    raise ScheduleError('任务写入后无法确认已注册')
            except (OSError, ScheduleError, KeyboardInterrupt) as failure:
                try:
                    if previous is not None:
                        self.register_windows(previous)
                        if not self.status():
                            raise ScheduleError('无法确认旧任务已恢复')
                    elif self.status():
                        result = self.command(['schtasks', '/Delete', '/F', '/TN', self.task])
                        if result.returncode:
                            raise ScheduleError('无法移除本次创建的任务')
                except (OSError, ScheduleError, KeyboardInterrupt):
                    if previous is not None:
                        recovery = self.stage_file(self.config.with_suffix('.task.xml'), previous)
                        raise ScheduleError(f'安装失败且回滚未完成；恢复文件：{recovery}') from failure
                    raise ScheduleError('安装失败且无法确认任务已清理，请检查任务计划程序') from failure
                if isinstance(failure, KeyboardInterrupt):
                    raise
                raise ScheduleError('安装失败，已恢复安装前的任务状态') from failure

    def register_windows(self, definition):
        fd, name = tempfile.mkstemp(suffix='.xml')
        try:
            with os.fdopen(fd, 'wb') as file:
                file.write(definition)
            result = self.command(['schtasks', '/Create', '/F', '/TN', self.task, '/XML', name])
            if result.returncode:
                raise ScheduleError('Windows 任务注册失败，请检查任务计划权限')
        finally:
            Path(name).unlink(missing_ok=True)

    def uninstall(self):
        self.supported()
        if self.status():
            args = (['launchctl','bootout',f'gui/{os.getuid()}/{self.label}']
                    if self.platform == 'darwin' else ['schtasks','/Delete','/F','/TN',self.task])
            if self.command(args).returncode: raise ScheduleError('卸载失败，保留配置供排查')
        if self.platform == 'darwin': self.plist.unlink(missing_ok=True)
