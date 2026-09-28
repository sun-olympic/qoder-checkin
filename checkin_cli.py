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
import subprocess
import sys
import tempfile
import uuid

import qoder_checkin as q
import qoder_auth
import qoder_notify as notify
from qoder_schedule import Scheduler, ScheduleError, BackgroundPermissionError


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


def check_background_access(scheduler, token_files):
    try:
        scheduler.preflight(token_files)
    except BackgroundPermissionError:
        if scheduler.platform != 'darwin' or not sys.stdin.isatty():
            raise
        print('后台访问预检失败；尚未重新注册任务。若是目录权限被拒绝，请手动授权后重测。')
        print('系统设置 → 隐私与安全 → 文件与文件夹：若有 Python 的文稿目录权限，请启用。')
        print('若无对应选项，可在完全磁盘访问权限中添加以下 Python；这会扩大所有使用该解释器的脚本权限：')
        print(str(Path(scheduler.argv[0]).resolve()))
        print('无需授权 launchd/xpcproxy。若不接受此权限，可取消后将项目移到非受保护目录再安装。')
        if input('回车打开权限设置并继续；输入 q 取消：').strip().lower() == 'q':
            raise ScheduleError('已取消，未重新注册任务')
        subprocess.run(['open', 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles'],
                       check=False, timeout=15)
        if input('完成授权后回车重测；输入 q 取消：').strip().lower() == 'q':
            raise ScheduleError('已取消，未重新注册任务')
        scheduler.preflight(token_files)
    if scheduler.platform == 'darwin':
        print('后台访问预检通过（未领取、未推送）。启动日志：' + str(scheduler.startup_log))


def wizard_accounts(config, directory):
    rows = config.setdefault('accounts', [])
    while True:
        print('当前账号：' + ('、'.join(f"{r['name']} ({r['region']})" for r in rows) or '无'))
        choice = input('账号管理：a 添加账号 / n 设置显示名称 / 回车继续设置：').strip().lower()
        if not choice:
            return
        if choice == 'n':
            target = input('请输入要设置的内部账号标识：').strip()
            account = next((r for r in rows if r['name'] == target), None)
            if account is None:
                raise q.ConfigError('账号不存在')
            label = input('自定义显示名称（留空使用登录昵称）：').strip()
            if len(label) > 64 or any(ord(c) < 32 for c in label):
                raise q.ConfigError('显示名称必须为不超过 64 字的单行文本')
            account['display_name'] = label
            continue
        if choice != 'a':
            raise q.ConfigError('账号管理请输入 a、n 或直接回车')
        label = input('自定义账号名称（留空使用登录昵称，最多 64 字）：').strip()
        if len(label) > 64 or any(ord(c) < 32 for c in label):
            raise q.ConfigError('显示名称必须为不超过 64 字的单行文本')
        name = label if re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', label) else 'account-' + uuid.uuid4().hex[:12]
        if any(r['name'] == name for r in rows):
            raise q.ConfigError('账号名称无效或重复；已有账号未修改')
        region = input('地区 global / cn [global]：').strip() or 'global'
        if region not in q.BASES:
            raise q.ConfigError('地区无效')
        mode = input('凭证方式：1 浏览器登录 / 2 手动 Token [1]：').strip() or '1'
        if mode not in ('1', '2'):
            raise q.ConfigError('凭证方式必须为 1 或 2')
        identity = uuid.uuid4().hex
        credential = directory / f'credentials-{name}-{identity}.json'
        account = {'name':name, 'region':region, 'token_file':credential.name,
                   'display_name':label,
                   'token_env':'QODER_ACCOUNT_' + identity.upper() + '_TOKEN',
                   'auth_source':'file', 'api_mode':'campaigns'}
        with tempfile.TemporaryDirectory(prefix='.qoder-add-', dir=directory) as tmp:
            candidate = Path(tmp) / 'credential.json'
            if mode == '1':
                print('请在浏览器中选择新账号；若自动登录旧账号，请先退出网站登录后重试。')
                result = qoder_auth.browser_login(region, candidate, isolated=True)
                uid = result['account_id']
                if any(r.get('region') == region and r.get('expected_account_id') == uid for r in rows):
                    raise q.ConfigError('该登录账号已绑定；请切换浏览器账号后重新添加')
                account['expected_account_id'] = uid
            else:
                token = getpass('Bearer Token（不回显）：').strip()
                if not token:
                    raise q.ConfigError('Token 不能为空')
                q.private_json(candidate, {'token':token})
            q.Account(name, region, q.BASES[region], '', candidate).token()
            candidate.chmod(0o600)
            os.link(candidate, credential)
        rows.append(account)
        config.setdefault('schedule', {})['region'] = 'both'


def wizard(args):
    print('Qoder 配置向导：API 模式无需保持客户端打开；可通过浏览器登录获取 Bearer Token。')
    if args.config.exists():
        config = read_config(args.config)
        print('保留现有账号和凭证配置，可继续添加账号。')
    else:
        kind = input('地区 global / cn / both [global]：').strip() or 'global'
        if kind not in ('global','cn','both'): raise q.ConfigError('地区无效')
        config = {'accounts':[]}
        auth_mode = input('凭证方式：1 浏览器登录（推荐） / 2 手动输入 Token / 3 使用环境变量 [1]：').strip() or '1'
        if auth_mode not in ('1', '2', '3'):
            raise q.ConfigError('凭证方式必须为 1、2 或 3')
        for region in (q.BASES if kind == 'both' else [kind]):
            credential = args.config.parent / f'credentials-{region}.json'
            if credential.exists():
                print(f'复用已有 {credential.name}，不会覆盖。')
            elif auth_mode == '1':
                try:
                    print(f'正在打开 Qoder {region} 登录页面，请在浏览器中完成登录……')
                    qoder_auth.browser_login(region, credential)
                    print(f'{region} 浏览器登录完成。')
                except (RuntimeError, OSError) as exc:
                    raise q.ConfigError(f'{region} 浏览器登录失败：{exc}') from None
            elif auth_mode == '2':
                token = getpass(f'{region} Bearer Token（不回显；留空使用环境变量）：').strip()
                if token: q.private_json(credential, {'token':token})
            config['accounts'].append({'name':region,'region':region,
                'token_env':f'QODER_{region.upper()}_TOKEN','token_file':credential.name,
                'api_mode':'campaigns','auth_source':'file'})
            if credential.exists():
                try:
                    saved = json.loads(credential.read_text())
                    if isinstance(saved, dict) and isinstance(saved.get('account_id'), str):
                        config['accounts'][-1]['expected_account_id'] = saved['account_id']
                except ValueError:
                    pass
    wizard_accounts(config, args.config.parent)
    at = input(f"每日时间，电脑本地时区 [{config.get('schedule',{}).get('at','10:05')}]：").strip()
    at = at or config.get('schedule',{}).get('at','10:05')
    validate_time(at)
    config.setdefault('schedule', {})['at'] = at
    current = '微信测试号' if config.get('notify_channel') == 'wx_test' else '未启用'
    print('当前通知：' + current)
    choice = input('通知：1 保留现状 / 2 浏览器扫码绑定微信 / 3 关闭通知 / 4 手动配置 [1]：').strip() or '1'
    if choice == '2':
        config = configure_wechat_browser(config)
    elif choice == '4':
        config = configure_wechat(config)
    elif choice == '3':
        config['notify_channel'] = 'none'
    elif choice != '1':
        raise q.ConfigError('通知选项必须为 1、2、3 或 4；配置未保存')
    save_config(args.config, config)
    if sys.stdin.isatty():
        check_background_access(Scheduler(args.config),
            [a.token_file for a in q.load_accounts(args.config, 'both') if a.token_file])
    print('配置已保存。运行 doctor 检查，再执行 install 注册定时任务。')
    if config.get('notify_channel') == 'wx_test':
        if choice in ('2', '4'):
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


def configure_wechat_browser(config):
    """Run the browser worker in its own runtime without restarting the wizard."""
    script = q.ROOT / 'qoder_wechat.py'
    try:
        with tempfile.TemporaryDirectory(prefix='qoder-wx-') as directory:
            source, output = Path(directory)/'input.json', Path(directory)/'output.json'
            q.private_json(source, config)
            arguments = ['--input', str(source), '--output', str(output)]
            command = qoder_auth.bootstrap_command(script, arguments) or [sys.executable, str(script), *arguments]
            result = subprocess.run(command, timeout=660)
            if result.returncode == 130:
                raise KeyboardInterrupt
            if result.returncode != 0 or not output.exists():
                raise q.ConfigError('微信扫码绑定未完成，原配置未修改')
            binding = json.loads(output.read_text())
            candidate = dict(config)
            candidate.update({key:binding.get(key) for key in (*notify.FIELDS, 'notify_channel')})
            if candidate.get('notify_channel') != 'wx_test':
                raise q.ConfigError('微信绑定结果无效，原配置未修改')
            notify.validate(candidate)
            return candidate
    except (OSError, ValueError, subprocess.SubprocessError):
        raise q.ConfigError('微信浏览器组件或绑定过程失败，原配置未修改') from None


def wx_bind(args):
    candidate = configure_wechat_browser(read_config(args.config))
    save_config(args.config, candidate)
    print('微信测试号绑定成功，已自动获取关注者和模板。可运行 test-notify 验证。')
    return 0


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


def qoder_login(args):
    """Human-assisted browser login; does not require Qoder IDE."""
    config = read_config(args.config) if args.config.exists() else {}
    region = args.login_region or "global"
    rows = config.get('accounts', [])
    matches = [row for row in rows if row.get('region') == region]
    if len(matches) > 1:
        raise q.ConfigError('同地区有多个账号，请使用独立配置登录，避免覆盖其他账号')
    account = matches[0] if matches else {'name': region, 'region': region}
    credential = args.config.parent / account.get('token_file', f'credentials-{region}.json')
    try:
        result = qoder_auth.browser_login(region, credential,
                                         expected_account_id=account.get('expected_account_id'))
    except (RuntimeError, OSError) as exc:
        raise q.ConfigError(str(exc)) from None
    if not matches:
        rows.append(account)
    account.setdefault('token_env', f'QODER_{region.upper()}_TOKEN')
    account.update({"token_file": str(credential.resolve()), "api_mode": "campaigns",
                    "auth_source": "file", 'expected_account_id':result['account_id']})
    config["accounts"] = rows
    save_config(args.config, config)
    print(f"{region} 登录成功，凭证已保存。可运行 doctor / install。")
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
    parser.add_argument('command', choices=['wizard','doctor','install','uninstall','delete-config','status','run','login','wx-bind','wx-setup','test-notify'])
    parser.add_argument('--config', type=Path, default=q.ROOT/'config.json')
    parser.add_argument('--region', choices=['both','global','cn'],default=None)
    parser.add_argument('--at', help='系统定时任务时间，电脑本地时区，默认 10:05')
    parser.add_argument('--dry-run', action='store_true',help='run 只查询，不领取')
    parser.add_argument('--login-region', choices=['global', 'cn'], default=None)
    args=parser.parse_args(argv)
    args.config=args.config.expanduser().resolve()
    # pythonw has no console; scheduled results still go to the rotating file log.
    for name in ('stdout','stderr'):
        if getattr(sys,name) is None: setattr(sys,name,open(os.devnull,'w',encoding='utf-8'))
    try:
        with ExitStack() as management:
            if args.command in ('wizard', 'install', 'uninstall', 'delete-config', 'login', 'wx-bind', 'wx-setup'):
                args.config.parent.mkdir(parents=True, exist_ok=True)
                lock_target = args.config.with_name(args.config.name + '.setup')
                if not management.enter_context(run_lock(lock_target)):
                    raise q.ConfigError('已有配置或安装操作运行，请等待完成后重试')
            explicit_region = args.region
            args.region = args.region or 'both'
            if args.command == 'delete-config':
                if not management.enter_context(run_lock(args.config)):
                    raise q.ConfigError('已有签到任务运行，请等待完成后再删除配置')
                if not args.config.exists():
                    print('配置不存在，无需删除。')
                    return 0
                if sys.platform in ('darwin', 'win32') and Scheduler(args.config).status():
                    raise q.ConfigError('定时任务仍已注册，请先对同一 --config 执行 uninstall，再删除配置')
                args.config.unlink()
                print('配置已删除（未放入废纸篓）；保留凭证、登录缓存、任务标识和日志。可运行 wizard 重新配置。')
                return 0
            if args.command=='wx-bind': return wx_bind(args)
            if args.command=='wx-setup': return wx_setup(args)
            if args.command=='login': return qoder_login(args)
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
                check_background_access(scheduler,
                    [a.token_file for a in q.load_accounts(args.config, args.region)])
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
