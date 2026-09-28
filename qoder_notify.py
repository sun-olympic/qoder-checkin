"""WeChat test-account notifications; never send Qoder credentials or raw errors."""
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request

from qoder_checkin import BEIJING, ConfigError

FIELDS = ('wx_test_appid', 'wx_test_secret', 'wx_test_touser', 'wx_test_template_id')
TEMPLATE = '结果：{{keyword1.DATA}}\n说明：{{keyword2.DATA}}\n时间：{{keyword3.DATA}}'


class NotifyError(ConfigError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NotifyError('微信接口发生重定向，已停止发送')


def request(url, payload=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except NotifyError:
        raise
    except Exception:
        raise NotifyError('微信接口请求失败；请检查网络和微信配置（未输出密钥）') from None


def validate(config):
    channel = config.get('notify_channel', 'none')
    if channel not in ('none', 'wx_test'):
        raise NotifyError('notify_channel 仅支持 none / wx_test')
    if channel == 'wx_test':
        for key in FIELDS:
            value = config.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise NotifyError('微信测试号配置不完整或无效：' + key)
    return channel


def api_error(stage, data):
    code = data.get('errcode')
    safe_code = str(code) if type(code) is int else 'unknown'
    return NotifyError(f'微信{stage}失败，错误码 {safe_code}；请检查测试号配置及接口权限')


def access_token(config):
    params = urllib.parse.urlencode({'grant_type': 'client_credential',
                                    'appid': config['wx_test_appid'],
                                    'secret': config['wx_test_secret']})
    result = request('https://api.weixin.qq.com/cgi-bin/token?' + params)
    token = result.get('access_token')
    if not isinstance(token, str) or not token or result.get('errcode', 0) != 0:
        raise api_error('认证', result)
    return token


def send(config, title, content):
    if validate(config) != 'wx_test':
        raise NotifyError('未启用微信通知，请运行 wx-setup')
    payload = {'touser': config['wx_test_touser'], 'template_id': config['wx_test_template_id'],
               'data': {'keyword1': {'value': title}, 'keyword2': {'value': content},
                        'keyword3': {'value': datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M:%S')}}}
    for attempt in range(2):
        token = access_token(config)
        response = request('https://api.weixin.qq.com/cgi-bin/message/template/send?' +
                           urllib.parse.urlencode({'access_token': token}), payload)
        if type(response.get('errcode')) is int and response['errcode'] == 0:
            return
        # Retry only explicit rejection for an expired/invalid WeChat token.
        if attempt == 0 and response.get('errcode') in (40001, 40014, 42001):
            continue
        raise api_error('推送', response)


def save_state(path, state):
    fd, name = tempfile.mkstemp(prefix='.qoder-notify-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def notify_results(config, config_path, results):
    """Caller holds the per-config run lock. Persist only hashes, never credentials."""
    if validate(config) == 'none':
        return 'disabled'
    # Each execution reports its result by default, including already-claimed.
    # Legacy cycle deduplication is available only through explicit opt-in.
    if config.get('notify_dedupe') is not True:
        failures = 0
        for item in results:
            title, content = result_message(item)
            try:
                send(config, title, content)
            except NotifyError:
                failures += 1
        if failures:
            raise NotifyError(f'{failures} 个账号的微信通知发送失败；其余账号已尝试推送，请检查微信配置或网络')
        return 'accepted' if results else 'empty'
    path = config_path.with_name(config_path.name + '.notify-state.json')
    try:
        state = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(state, dict):
            raise ValueError()
    except (OSError, ValueError, RecursionError):
        raise NotifyError('通知去重记录不可读；未发送，请检查 .notify-state.json') from None
    # The daily campaign resets at 10:00 Beijing time, not midnight.
    cycle = (datetime.now(BEIJING) - timedelta(hours=10)).date().isoformat()
    recipient = hashlib.sha256(json.dumps([config[k] for k in FIELDS if k != 'wx_test_secret']).encode()).hexdigest()
    pending = []
    for item in results:
        outcome = item.get('result', 'error')
        outcome = 'claimed' if outcome == 'already_claimed' else outcome
        signature = [cycle, recipient, item.get('account'), item.get('region'),
                     item.get('campaign_id'), outcome, item.get('error_type')]
        key = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        if state.get(key) == cycle:
            continue
        # Do not forward arbitrary exception strings or API responses.
        title, content = result_message(item)
        pending.append((key, title, content))
    if not pending:
        return 'duplicate'
    state = {k: v for k, v in state.items() if v == cycle}
    # One message per account avoids long template values being truncated.
    for key, title, content in pending:
        send(config, title, content)
        state[key] = cycle
        try:
            save_state(path, state)
        except OSError:
            raise NotifyError('微信已受理，但通知去重记录保存失败；下次运行可能重复通知') from None
    return 'accepted'


def result_message(item):
    """WorkBuddy-style account notices, using only safe status/error categories."""
    outcome = item.get('result')
    messages = {
        'claimed': ('🎉 Qoder 自动签到成功', '本次自动签到已完成'),
        'already_claimed': ('✅ Qoder 本轮已签到', '检测到本轮奖励此前已领取，本次未重复领取'),
        'claimable': ('📋 Qoder 签到状态查询完成', '本轮奖励可领取；本次仅查询，未执行签到'),
        'unavailable': ('⏳ Qoder 暂不可签到', '当前暂无可领取奖励，请以活动开放时间和资格为准'),
    }
    title, detail = messages.get(outcome, ('⚠️ Qoder 签到状态未知', '请检查本机日志'))
    if outcome == 'error':
        title = '❌ Qoder 签到失败'
        detail = {
            'AuthError': '登录凭证失效或账号校验未通过，请重新登录并检查账号绑定',
            'ConfigError': '账号配置或凭证缺失，请检查本机配置',
            'ApiError': '接口请求失败，请检查网络及本机日志',
        }.get(item.get('error_type'), '签到未完成，请检查本机日志')
    region = {'global': '国际站', 'cn': '中国站'}.get(item.get('region'), '未知地区')
    content = f'{region}：{detail}'
    credits = item.get('rewardCredits')
    if outcome in ('claimed', 'already_claimed', 'claimable') and type(credits) in (int, float) and math.isfinite(credits) and credits >= 0:
        content += f'；本轮奖励：{credits} Credits'
    return f"[{item.get('display_name') or item.get('account', '账号')}] {title}", content


def binding_options(config):
    """Read candidates through official APIs; never pick an arbitrary recipient."""
    token = access_token(config)
    query = urllib.parse.urlencode({'access_token': token})
    followers = request('https://api.weixin.qq.com/cgi-bin/user/get?' + query)
    if followers.get('errcode', 0) != 0:
        raise api_error('获取关注者', followers)
    data = followers.get('data', {})
    users = data.get('openid', []) if isinstance(data, dict) else None
    if not isinstance(users, list) or any(not isinstance(v, str) or not v for v in users):
        raise NotifyError('微信关注者响应格式无效')
    # Do not silently select the only item on a partial page.
    total = followers.get('total', len(users))
    if type(total) is not int or total != len(users):
        raise NotifyError('关注者列表未完整返回，请使用手动输入 OpenID')
    templates = request('https://api.weixin.qq.com/cgi-bin/template/get_all_private_template?' + query)
    if templates.get('errcode', 0) != 0:
        raise api_error('获取模板', templates)
    rows = templates.get('template_list', [])
    if not isinstance(rows, list):
        raise NotifyError('微信模板响应格式无效')
    matching = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        content = row.get('content')
        tid = row.get('template_id')
        if isinstance(content, str) and isinstance(tid, str) and tid and content.strip().replace('\r\n', '\n') == TEMPLATE:
            matching.append(tid)
    return list(dict.fromkeys(users)), list(dict.fromkeys(matching))
