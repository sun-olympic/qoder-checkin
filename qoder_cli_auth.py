"""Normal-browser login through the pinned official CLI, without an IDE."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

import qoder_checkin as q

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / '.qoder-cli-runtime'
VERSION = '1.1.64'
ARCHIVE_SHA = '68287d5a64318584defdf0476322ec4a2c284db06a9089eb6950c19ba70415a9'
BINARY_SHA = '56f350899efd7fe296f197dd4889ae685860da915f958c51a51ac32c0daa224b'
URL = f'https://qoder-ide.oss-accelerate.aliyuncs.com/qodercli/releases/{VERSION}/qodercli-darwin-arm64.tar.gz'


def digest(path):
    with path.open('rb') as stream:
        value = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
        return value.hexdigest()


def ensure_cli():
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise q.ConfigError('官方 CLI 自动导入目前已验证 macOS Apple Silicon；其他平台请暂用手动 Token')
    RUNTIME.mkdir(parents=True, exist_ok=True)
    binary = RUNTIME / 'qodercli'
    if binary.is_file() and digest(binary) == BINARY_SHA:
        return binary
    print(f'首次准备官方 Qoder CLI {VERSION}（无需 IDE）……', flush=True)
    with tempfile.TemporaryDirectory(dir=RUNTIME) as directory:
        archive = Path(directory) / 'cli.tar.gz'
        with urllib.request.urlopen(URL, timeout=60) as response, archive.open('wb') as out:
            shutil.copyfileobj(response, out)
        if digest(archive) != ARCHIVE_SHA:
            raise q.ConfigError('官方 CLI 下载校验失败，未执行')
        candidate = Path(directory) / 'qodercli'
        with tarfile.open(archive) as package:
            member = next((m for m in package.getmembers() if m.name.lstrip('./') == 'qodercli' and m.isfile()), None)
            if member is None:
                raise q.ConfigError('官方 CLI 安装包格式不匹配')
            with package.extractfile(member) as source, candidate.open('wb') as out:
                shutil.copyfileobj(source, out)
        if digest(candidate) != BINARY_SHA:
            raise q.ConfigError('官方 CLI 程序校验失败，未执行')
        candidate.chmod(0o700)
        os.replace(candidate, binary)
    return binary


def account_session_dir(token_file):
    """Persistent per-account CLI session: keeps the refresh token so the CLI can renew without a browser."""
    return RUNTIME / 'sessions' / Path(token_file).stem


def browser_login(region, output, timeout_seconds=300, expected_account_id=None, isolated=False,
                  session_dir=None, interactive=True):
    """Import a credential from an official-CLI session.

    A cached session is first run through `status` (the CLI refreshes an expired or near-expiry token
    on startup), then imported. interactive=False never opens a browser: it only renews from a cached session.
    """
    if region != 'global':
        raise q.ConfigError('国内版 CLI 授权尚未适配，请选择手动 Token；不会跳转国际版登录')
    node = shutil.which('node')
    if not node:
        raise q.ConfigError('读取官方 CLI 凭证需要 Node.js，请先安装 Node.js')
    config = RUNTIME / 'auth-global'
    if isolated:
        # The caller's staging directory is unique and cleaned after import.
        config = Path(output).resolve().parent / 'cli-session'
    if session_dir:
        config = Path(session_dir)
    session = config / '.auth/user'
    if not interactive and not session.is_file():
        raise q.AuthError('没有可用的官方 CLI 登录态，无法自动续期')
    binary = ensure_cli()
    environment = dict(os.environ)
    # Prevent external terminal credentials from silently changing login identity.
    for name in ('QODER_PERSONAL_ACCESS_TOKEN', 'QODER_CONFIG_DIR'):
        environment.pop(name, None)

    def login():
        print('请在系统浏览器完成 Qoder 授权；完成后自动验证签到接口。', flush=True)
        result = subprocess.run([str(binary), '--config-dir', str(config), 'login'],
                                env=environment, timeout=timeout_seconds)
        if result.returncode:
            raise q.AuthError('官方 CLI 登录未完成；原凭证未修改')

    def import_credential():
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.qoder-auth-', dir=target.parent) as directory:
            candidate = Path(directory) / 'credential.json'
            imported = subprocess.run([node, str(ROOT/'import_cli_auth.mjs'), str(binary),
                                       str(config), str(candidate)], capture_output=True, timeout=30)
            if imported.returncode:
                raise q.AuthError('官方 CLI 凭证导入失败；原凭证未修改')
            data = json.loads(candidate.read_text())
            uid = data.get('account_id')
            if not isinstance(uid, str) or not uid:
                raise q.AuthError('官方 CLI 缺少账号身份；原凭证未修改')
            if expected_account_id and uid != expected_account_id:
                raise q.AuthError('登录账号与绑定账号不一致；原凭证未修改')
            account = q.Account('login', region, q.BASES[region], '', candidate,
                                expected_account_id=uid)
            rows = q.CampaignClient(q.Transport(q.BASES[region], account.token()), uid).campaigns()
            candidate.chmod(0o600)
            os.replace(candidate, target)
            return {'account_id': uid, 'campaign_count': len(rows), 'source': 'qoder-cli'}

    try:
        reused = session.is_file()
        if reused:
            if interactive:
                print('发现本项目的官方 CLI 登录态，先验证并导入。')
            # Exit code ignored on purpose: a failed refresh surfaces as an expired token at import.
            subprocess.run([str(binary), '--config-dir', str(config), 'status'],
                           env=environment, capture_output=True, timeout=60)
        else:
            login()
        try:
            return import_credential()
        except q.AuthError as exc:
            if not reused or not interactive:
                raise
            # ponytail: any AuthError on a cached session is treated as stale (expired token, wrong
            # account, unreadable); clear it and authorize once. Ceiling: no finer-grained reason check.
            print(f'{exc}；已清除失效的登录态，重新打开浏览器授权。', flush=True)
            session.unlink(missing_ok=True)
        login()
        return import_credential()
    except subprocess.TimeoutExpired:
        raise q.AuthError('官方 CLI 授权或导入超时；原凭证未修改') from None
