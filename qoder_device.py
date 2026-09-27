"""Read this Mac's genuine Qoder identity through the installed vendor helper.

Does not launch Electron or synthesize device identifiers. Never log helper output.
"""
from __future__ import annotations
import json
from pathlib import Path
import platform
import plistlib
import re
import socket
import subprocess
import sys


class DeviceError(Exception):
    pass


DEVICE_HEADERS = frozenset({'Cosy-MachineId', 'Cosy-MachineToken', 'Cosy-MachineCode',
                            'Cosy-MachineType', 'Cosy-MachineOS', 'Cosy-MachineHostname',
                            'Cosy-Version'})


def validate_headers(headers):
    if not isinstance(headers, dict) or any(key not in DEVICE_HEADERS for key in headers):
        raise DeviceError('设备请求头格式无效')
    for value in headers.values():
        if not isinstance(value, str) or not re.fullmatch(r'[\x21-\x7e](?:[\x20-\x7e]{0,4094}[\x21-\x7e])?', value):
            raise DeviceError('设备请求头包含无效值')
    return headers


def native_headers(region, account_id, app_path=None):
    if sys.platform != 'darwin':
        raise DeviceError('Qoder 设备信息自动读取目前仅支持 macOS')
    if region not in ('global','cn') or not isinstance(account_id,str) or not account_id:
        raise DeviceError('设备信息需要绑定 Qoder 账号')
    if not app_path and region != 'global':
        raise DeviceError('国内版设备信息尚未实测，请显式配置对应 qoder_app_path')
    app = Path(app_path or '/Applications/Qoder.app')
    resources = app/'Contents/Resources'
    directory = 'com.qoder.app.stable' if region == 'global' else 'com.qodercn.app.stable'
    identity = Path.home()/'Library/Application Support'/directory/'auth.machine-id'
    try:
        machine_id = identity.read_text(encoding='utf-8').strip()
        if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', machine_id):
            raise DeviceError('Qoder 本机设备 ID 无效，请在官方客户端完成首次登录')
        with (app/'Contents/Info.plist').open('rb') as file:
            version = plistlib.load(file)['CFBundleShortVersionString']
        result = subprocess.run([str(resources/'umid/runtime-info'), '3' if region=='global' else '0',
                                 '--account-stdin'], input=json.dumps({'account':account_id})+'\n',
                                capture_output=True, text=True, timeout=30)
        if result.returncode or len(result.stdout)>1024*1024:
            raise DeviceError('Qoder 官方设备组件执行失败')
        data = json.loads(result.stdout.splitlines()[0])
        if not isinstance(data,dict):raise DeviceError('Qoder 官方设备组件返回格式无效')
        headers = {'Cosy-MachineId':machine_id, 'Cosy-Version':version,
                   'Cosy-MachineOS':{'arm64':'aarch64','x86_64':'x86_64'}.get(platform.machine(),platform.machine())+'_darwin'}
        host=socket.gethostname()
        # The hostname header is optional; omit unsupported characters rather than spoofing it.
        if re.fullmatch(r'[\x21-\x7e]{1,96}',host):headers['Cosy-MachineHostname']=host
        for field in ('machineToken','machineCode','machineType'):
            headers['Cosy-'+field[0].upper()+field[1:]]=data[field]
        return validate_headers(headers)
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.TimeoutExpired):
        raise DeviceError('无法获取 Qoder 本机设备信息；请检查安装路径和首次登录状态，未启动 IDE') from None
