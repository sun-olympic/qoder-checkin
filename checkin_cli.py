#!/usr/bin/env python3
"""Setup and schedule Qoder check-in; no third-party Python dependencies."""
from __future__ import annotations
import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime
from getpass import getpass
import json
import errno
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

import qoder_checkin as q
import qoder_notify as notify
from qoder_schedule import Scheduler, ScheduleError


def save_config(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.qoder-config-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write('\n')
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_config(path):
    if not path.exists(): raise q.ConfigError('请先执行 wizard 创建配置')
    q.load_accounts(path, 'both')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data.get('schedule', {}), dict):
        raise q.ConfigError('schedule 必须是 JSON 对象')
    return data


@contextmanager
def run_lock(config):
    """Kernel releases lock on crash; never delete the lock inode."""
    fd = os.open(config.with_name(config.name + '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    file = os.fdopen(fd, 'r+b')
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            # Windows permits byte-range locks beyond EOF; never read a byte
            # another process may already hold exclusively.
            file.seek(0)
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as exc:
                winerror = getattr(exc, 'winerror', None)
                contention = (winerror == 33 if winerror is not None else
                              exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK))
                if not contention:
                    raise
        else:
            import fcntl
            try: fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB); acquired = True
            except BlockingIOError: pass
        yield acquired
    finally:
        file.close()


def run(args):
    config = read_config(args.config)
    with run_lock(args.config) as acquired:
        if not acquired:
            print('已有领取任务运行，本次跳过')
            return 0
        results = q.run_once(args.config, args.region, 'status' if args.dry_run else 'claim', 20)
        q.emit(results, True)
        logging.getLogger('qoder.results').info(json.dumps(
            {'time':datetime.now(q.BEIJING).isoformat(), 'accounts':results}, ensure_ascii=False))
        if not args.dry_run:
            try:
                notification = notify.notify_results(config, args.config, results)
                if notification != 'disabled':
                    logging.getLogger('qoder.results').info(json.dumps(
                        {'time':datetime.now(q.BEIJING).isoformat(), 'notification':notification}))
            except notify.NotifyError as exc:
                logging.getLogger('qoder.results').error(json.dumps(
                    {'time':datetime.now(q.BEIJING).isoformat(), 'notification':'failed',
                     'message':str(exc)}, ensure_ascii=False))
                print(str(exc), file=sys.stderr)
                return q.exit_code(results) or 1
        return q.exit_code(results)


class StrictRotatingFileHandler(RotatingFileHandler):
    """Let the failover handler observe errors normally swallowed by logging."""
    def handleError(self, record):
        raise q.ConfigError('日志写入或轮换失败') from None

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, 'a', encoding='utf-8')


class FailoverLogHandler(logging.Handler):
    def __init__(self, paths):
        super().__init__()
        self.paths = iter(paths)
        self.active = None
        self.baseFilename = ''
        self.exhausted = False
        self._advance()

    def _advance(self):
        if self.active is not None:
            try:
                self.active.close()
            except OSError:
                pass
            self.active = None
        for path in self.paths:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                handler = StrictRotatingFileHandler(path, maxBytes=1024*1024,
                                                     backupCount=3, encoding='utf-8')
                try:
                    if os.name != 'nt': path.chmod(0o600)
                except OSError:
                    handler.close()
                    raise
                self.active = handler
                self.baseFilename = handler.baseFilename
                return
            except OSError:
                continue
        self.exhausted = True
        raise q.ConfigError('主日志和备用日志均不可写；请检查项目目录及 ~/.qoder-checkin/logs 权限')

    def emit(self, record):
        while self.active is not None:
            try:
                self.active.emit(record)
                return
            except (OSError, q.ConfigError):
                # Retry only the log record, never the check-in API operation.
                self._advance()
        raise q.ConfigError('没有可用的运行日志')

    def close(self):
        try:
            if self.active is not None:
                self.active.close()
                self.active = None
        finally:
            super().close()


def run_logged(args):
    # Set up durable diagnostics before parsing config or acquiring the lock.
    key = hashlib.sha256(str(args.config).encode()).hexdigest()[:12]
    paths = [args.config.parent / 'checkin.log',
             Path.home() / '.qoder-checkin' / 'logs' / (key + '.log')]
    handler = FailoverLogHandler(paths)
    logger = logging.getLogger('qoder.results')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        return run(args)
    except Exception as exc:
        # Never serialize arbitrary exceptions or tracebacks containing credentials.
        message = (str(exc) if isinstance(exc, (q.CheckinError, ScheduleError))
                   else '运行失败，请检查本地配置、文件权限和运行环境')
        if not handler.exhausted:
            logger.error(json.dumps({'time':datetime.now(q.BEIJING).isoformat(),
                                    'result':'error', 'error_type':type(exc).__name__,
                                    'message':message}, ensure_ascii=False))
        print(message + '；日志：' + handler.baseFilename, file=sys.stderr)
        return 2
    finally:
        logger.removeHandler(handler)
        handler.close()


def doctor(args):
    print(f'Python {sys.version.split()[0]} | 平台 {sys.platform}')
    print('原生定时任务：' + ('支持' if sys.platform in ('darwin','win32') else '使用 daemon / CI'))
    print('Node.js（仅 macOS 凭证导入需要）：' + ('已找到' if shutil.which('node') else '未找到'))
    accounts = q.load_accounts(args.config, args.region)
    ok = True
    for account in accounts:
        try:
            account.token()
            message = '本地凭证格式有效（未向服务端验证）'
            if account.auth_source == 'macos' and (sys.platform != 'darwin' or not shutil.which('node')):
                raise q.ConfigError('macos 同步模式需要 macOS 和 Node.js')
        except q.CheckinError as exc:
            ok = False; message = str(exc)
        print(f'{account.name} / {account.region} / {account.auth_source}: {message}')
    channel = notify.validate(read_config(args.config))
    print('微信通知：' + ('已配置（未发送验证）' if channel == 'wx_test' else '未启用，可运行 wx-setup'))
    return 0 if ok else 1


def wizard(args):
    print('Qoder 配置向导：API 模式无需保持客户端打开；客户端自动导入目前仅支持 macOS App。')
    if args.config.exists():
        config = read_config(args.config)
        print('保留现有账号和凭证配置。新增账号可编辑 accounts 数组，名称必须唯一。')
    else:
        kind = input('地区 global / cn / both [global]：').strip() or 'global'
        if kind not in ('global','cn','both'): raise q.ConfigError('地区无效')
        config = {'accounts':[]}
        for region in (q.BASES if kind == 'both' else [kind]):
            credential = args.config.parent / f'credentials-{region}.json'
            if credential.exists():
                print(f'复用已有 {credential.name}，不会覆盖。')
            else:
                token = getpass(f'{region} Bearer Token（不回显；留空使用环境变量）：').strip()
                if token: q.private_json(credential, {'token':token})
            config['accounts'].append({'name':region,'region':region,
                'token_env':f'QODER_{region.upper()}_TOKEN','token_file':credential.name,
                'api_mode':'campaigns','auth_source':'file'})
    at = input(f"每日时间，电脑本地时区 [{config.get('schedule',{}).get('at','10:05')}]：").strip()
    at = at or config.get('schedule',{}).get('at','10:05')
    validate_time(at)
    config.setdefault('schedule', {})['at'] = at
    current = '微信测试号' if config.get('notify_channel') == 'wx_test' else '未启用'
    print('当前通知：' + current)
    choice = input('通知：1 保留现状 / 2 配置微信测试号 / 3 关闭通知 [1]：').strip() or '1'
    if choice == '2':
        config = configure_wechat(config)
    elif choice == '3':
        config['notify_channel'] = 'none'
    elif choice != '1':
        raise q.ConfigError('通知选项必须为 1、2 或 3；配置未保存')
    save_config(args.config, config)
    print('配置已保存。运行 doctor 检查，再执行 install 注册定时任务。')
    if config.get('notify_channel') == 'wx_test':
        if choice == '2':
            offer_notification_test(config)
        else:
            print('运行 test-notify 验证微信推送。')
    return 0


def choose_wechat_value(values, existing, label):
    if existing and existing in values:
        print(label + '：已核对并保留当前配置')
        return existing
    if len(values) == 1:
        print(label + '：已找到唯一候选')
        return values[0]
    if len(values) > 1:
        print(label + '有多个候选，请对照测试号后台选择：')
        for index, value in enumerate(values, 1):
            print(f'  {index}. {value[:6]}…{value[-4:]}')
        choice = input('输入序号；留空手动填写：').strip()
        if choice:
            if not choice.isascii() or not choice.isdecimal() or len(choice) > 6 or not 1 <= int(choice) <= len(values):
                raise q.ConfigError('候选序号无效，配置未保存')
            return values[int(choice) - 1]
    return getpass(label + '（不回显）：').strip()


def configure_wechat(config):
    print('微信测试号配置向导：https://mp.weixin.qq.com/debug/cgi-bin/sandbox?t=sandbox/login')
    print('请自行在浏览器扫码登录，再扫码关注测试号。创建模板时使用以下内容：')
    print(notify.TEMPLATE)
    print('输入不回显；留空保留已有值。配置仅保存在本机。')
    candidate = dict(config)
    for key, label in zip(notify.FIELDS[:2], ('AppID', 'AppSecret')):
        value = getpass(label + '：').strip()
        if value:
            candidate[key] = value
    mode = input('绑定方式：1 API 自动查找关注者和模板 / 2 手动填写 [1]：').strip() or '1'
    if mode == '1':
        for key in notify.FIELDS[:2]:
            if not isinstance(candidate.get(key), str) or not candidate[key].strip():
                raise q.ConfigError('请填写 AppID 和 AppSecret')
        input('确认已关注测试号并创建模板后，按回车查询：')
        users, templates = notify.binding_options(candidate)
        for key, label, values in (
            ('wx_test_touser', '接收者 OpenID', users),
            ('wx_test_template_id', '模板 ID', templates),
        ):
            candidate[key] = choose_wechat_value(values, candidate.get(key), label)
    elif mode == '2':
        for key, label in zip(notify.FIELDS[2:], ('接收者 OpenID', '模板 ID')):
            value = getpass(label + '：').strip()
            if value:
                candidate[key] = value
    else:
        raise q.ConfigError('绑定方式必须为 1 或 2，配置未保存')
    candidate['notify_channel'] = 'wx_test'
    notify.validate(candidate)
    return candidate


def offer_notification_test(config):
    answer = input('现在发送一条微信测试通知？[y/N]：').strip().lower()
    if answer in ('y', 'yes'):
        try:
            notify.send(config, 'Qoder 通知测试', '微信通知已接入；本条消息不执行签到。')
        except notify.NotifyError:
            print('配置已保存，但测试推送失败；可修正配置后运行 test-notify 重试。')
            raise
        print('微信接口已受理，请在手机确认收到消息。')


def wx_setup(args):
    candidate = configure_wechat(read_config(args.config))
    save_config(args.config, candidate)
    print('微信配置已保存，原签到设置已保留。')
    offer_notification_test(candidate)
    return 0


def test_notify(args):
    config = read_config(args.config)
    notify.send(config, 'Qoder 通知测试', '微信通知已接入；本条消息不执行签到。')
    print('微信接口已受理测试消息，请在手机微信确认。')
    return 0


def validate_time(at):
    if not isinstance(at,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',at):
        raise q.ConfigError('时间必须为 HH:MM，例如 10:05')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Qoder 安装、诊断和系统定时任务')
    parser.add_argument('command', choices=['wizard','doctor','install','uninstall','status','run','wx-setup','test-notify'])
    parser.add_argument('--config', type=Path, default=q.ROOT/'config.json')
    parser.add_argument('--region', choices=['both','global','cn'],default=None)
    parser.add_argument('--at', help='系统定时任务时间，电脑本地时区，默认 10:05')
    parser.add_argument('--dry-run', action='store_true',help='run 只查询，不领取')
    args=parser.parse_args(argv)
    args.config=args.config.expanduser().resolve()
    # pythonw has no console; scheduled results still go to the rotating file log.
    for name in ('stdout','stderr'):
        if getattr(sys,name) is None: setattr(sys,name,open(os.devnull,'w',encoding='utf-8'))
    try:
        with ExitStack() as management:
            if args.command in ('wizard', 'install', 'uninstall', 'wx-setup'):
                args.config.parent.mkdir(parents=True, exist_ok=True)
                lock_target = args.config.with_name(args.config.name + '.setup')
                if not management.enter_context(run_lock(lock_target)):
                    raise q.ConfigError('已有配置或安装操作运行，请等待完成后重试')
            explicit_region = args.region
            args.region = args.region or 'both'
            if args.command=='wx-setup': return wx_setup(args)
            if args.command=='test-notify': return test_notify(args)
            if args.command=='wizard': return wizard(args)
            if args.command=='doctor': return doctor(args)
            if args.command=='run': return run_logged(args)
            if args.command=='uninstall':
                Scheduler(args.config).uninstall()
                print('定时任务已卸载；保留账号、凭证和日志。')
                return 0
            config=read_config(args.config)
            if args.command == 'install' and explicit_region is None:
                args.region = config.get('schedule', {}).get('region', 'both')
                if args.region not in ('both', 'global', 'cn'):
                    raise q.ConfigError('保存的 schedule.region 无效，请显式指定 --region')
            at=args.at or config.get('schedule',{}).get('at','10:05')
            validate_time(at)
            scheduler=Scheduler(args.config,args.region,at)
            if args.command=='install':
                if doctor(args): raise q.ConfigError('环境预检未通过，未注册任务')
                # No interactive keychain prompts in unattended native tasks.
                if any(a.auth_source=='macos' for a in q.load_accounts(args.config,args.region)):
                    raise q.ConfigError('请先导入凭证，再将 auth_source 设为 file，避免后台等待钥匙串弹窗')
                for a in q.load_accounts(args.config,args.region):
                    if not a.token_file or not a.token_file.exists():
                        raise q.ConfigError('系统任务需要 token_file；终端环境变量不会自动传给定时任务')
                    # Validate the file independently from an overriding terminal environment.
                    q.Account(a.name,a.region,a.base_url,'',a.token_file,
                              expected_account_id=a.expected_account_id).token()
                # Commit config before touching the OS, so a write failure cannot
                # leave a new task running with stale metadata. Keep an atomic backup.
                original = args.config.read_bytes()
                backup = Scheduler.stage_file(args.config, original)
                keep_backup = False
                try:
                    config['schedule']={'at':at,'region':args.region,'timezone':'local'}
                    try:
                        save_config(args.config,config)
                        scheduler.install()
                    except (OSError, ScheduleError, KeyboardInterrupt):
                        try:
                            os.replace(backup, args.config)
                        except (OSError, KeyboardInterrupt):
                            keep_backup = True
                            raise q.ConfigError(f'安装失败且配置恢复失败；恢复文件：{backup}') from None
                        raise
                finally:
                    if not keep_backup:
                        backup.unlink(missing_ok=True)
                print(f'已注册：每天本地时间 {at} + 登录补跑。无需保持终端或 Qoder 打开。')
            else:
                print('定时注册状态：'+('已注册' if scheduler.status() else '未注册'))
                print(scheduler.runtime_status())
                print(f'配置时间：{at}（电脑本地时区）；修改时间或移动目录后需重新 install。')
                print(f'配置：{args.config}\n日志：{args.config.parent / "checkin.log"}')
            return 0
    except (q.CheckinError,ScheduleError,OSError,ValueError) as exc:
        print(str(exc) if isinstance(exc,(q.CheckinError,ScheduleError)) else '本地文件或系统操作失败，请检查配置与权限',file=sys.stderr)
        return 2
    except (KeyboardInterrupt,EOFError): return 130


if __name__=='__main__':
    sys.exit(main())
