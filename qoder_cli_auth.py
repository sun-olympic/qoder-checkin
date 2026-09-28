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


def browser_login(region, output, timeout_seconds=300, expected_account_id=None, isolated=False):
    if region != 'global':
        raise q.ConfigError('国内版 CLI 授权尚未适配，请选择手动 Token；不会跳转国际版登录')
    node = shutil.which('node')
    if not node:
        raise q.ConfigError('读取官方 CLI 凭证需要 Node.js，请先安装 Node.js')
    binary = ensure_cli()
    config = RUNTIME / 'auth-global'
    if isolated:
        # The caller's staging directory is unique and cleaned after import.
        config = Path(output).resolve().parent / 'cli-session'
    environment = dict(os.environ)
    # Prevent external terminal credentials from silently changing login identity.
    for name in ('QODER_PERSONAL_ACCESS_TOKEN', 'QODER_CONFIG_DIR'):
        environment.pop(name, None)
    print('请在系统浏览器完成 Qoder 授权；完成后自动验证签到接口。', flush=True)
    try:
        if not (config / '.auth/user').is_file():
            result = subprocess.run([str(binary), '--config-dir', str(config), 'login'],
                                    env=environment, timeout=timeout_seconds)
            if result.returncode:
                raise q.AuthError('官方 CLI 登录未完成；原凭证未修改')
        else:
            print('发现本项目的官方 CLI 登录态，先验证并导入。')
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.qoder-auth-', dir=output.parent) as directory:
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
            os.replace(candidate, output)
            return {'account_id': uid, 'campaign_count': len(rows), 'source': 'qoder-cli'}
    except subprocess.TimeoutExpired:
        raise q.AuthError('官方 CLI 授权或导入超时；原凭证未修改') from None
