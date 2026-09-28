"""Browser binding adapted from workbuddy-checkin; secrets stay on disk or in memory."""
import argparse
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

import qoder_auth
import qoder_notify as notify
import qoder_checkin as q

URL = 'https://mp.weixin.qq.com/debug/cgi-bin/sandbox?t=sandbox/login'

EXTRACT_CREDS_JS = """() => {
    const res = {appid:'', appsecret:''};
    const inputs = Array.from(document.querySelectorAll('input'));
    for (const i of inputs) {
        const v = (i.value||'').trim();
        const key = ((i.id||'')+(i.name||'')+(i.placeholder||'')).toLowerCase();
        if (/appid|app_id|开发者id|开发者/i.test(key) && /^wx[0-9a-f]{8,}$/i.test(v)) res.appid = v;
        if (/appsecret|app_secret|开发者密码|密码/i.test(key) && /^[0-9a-f]{16,}$/i.test(v)) res.appsecret = v;
    }
    if (!res.appid) { const m = document.body.innerText.match(/wx[0-9a-f]{8,}/i); if (m) res.appid = m[0]; }
    if (!res.appsecret) { const m = document.body.innerText.match(/[0-9a-f]{32}/i); if (m) res.appsecret = m[0]; }
    return res;
}"""


def _click_new_template(page):
    """定位并点击新增测试模板入口。"""
    page.bring_to_front()
    for frame in page.frames:
        for locator in (
            frame.get_by_text("新增测试模板", exact=False).first,
            frame.locator("button:has-text('新增测试模板')").first,
            frame.locator("input[value*='新增测试模板']").first,
        ):
            try:
                if locator.count() and locator.is_visible(timeout=1000):
                    locator.scroll_into_view_if_needed()
                    locator.click(timeout=5000)
                    page.wait_for_timeout(800)
                    return True
            except Exception:
                continue
    return False


FILL_TEMPLATE_JS = """(args) => {
    const visible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
    const ownText = (el) => Array.from(el.childNodes)
        .filter(node => node.nodeType === Node.TEXT_NODE)
        .map(node => node.textContent || '').join('').trim();
    const validDialog = (el) => {
        if (!visible(el)) return false;
        const text = el.innerText || '';
        if (!/新增测试模板/.test(text) || !/模板标题/.test(text) || !/模板内容/.test(text)) return false;
        const hasInput = Array.from(el.querySelectorAll('input')).some(visible);
        const hasContent = Array.from(el.querySelectorAll('textarea, [contenteditable="true"]')).some(visible);
        return hasInput && hasContent;
    };

    // 从弹窗标题向上找包含完整表单的最近祖先。背景表单即使可见，也不会进入候选。
    const roots = [];
    const markers = Array.from(document.querySelectorAll('body *')).filter(el =>
        visible(el) && /新增测试模板/.test(ownText(el)));
    for (const marker of markers) {
        let node = marker;
        while (node && node !== document.body) {
            if (validDialog(node)) {
                roots.push(node);
                break;
            }
            node = node.parentElement;
        }
    }
    roots.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
    const root = roots[0];
    if (!root) return {title:false, content:false, submit:false, dialog:false};

    const describe = (el) => {
        const parent = el.closest('label, .weui-cell, .form-item, .control-group, tr, li, div');
        return [el.name, el.id, el.placeholder, el.getAttribute('aria-label'),
                parent ? parent.innerText.slice(0, 120) : ''].filter(Boolean).join(' ').toLowerCase();
    };
    const controls = Array.from(root.querySelectorAll('input, textarea, [contenteditable="true"]')).filter(visible);
    const title = controls.find(el => el.tagName === 'INPUT' && /模板?标题|title/.test(describe(el))) ||
                  controls.find(el => el.tagName === 'INPUT' && ['text', ''].includes(el.type || ''));
    const content = controls.find(el => el.tagName === 'TEXTAREA' && /模板?内容|content/.test(describe(el))) ||
                    controls.find(el => el.tagName === 'TEXTAREA') ||
                    controls.find(el => el.isContentEditable && /模板?内容|content/.test(describe(el)));
    const set = (el, value) => {
        if (!el) return false;
        el.focus();
        if (el.isContentEditable) el.textContent = value;
        else {
            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
        }
        for (const type of ['input', 'change', 'blur']) el.dispatchEvent(new Event(type, {bubbles:true}));
        return true;
    };
    const buttons = Array.from(root.querySelectorAll('button, input[type="submit"], input[type="button"], a')).filter(visible);
    const submit = buttons.find(el => /^(提交|确定|保存|添加)$/.test((el.innerText || el.value || '').trim()));
    if (submit) submit.setAttribute('data-qoder-template-submit', 'true');
    return {
        title: set(title, args.title),
        content: set(content, args.content),
        submit: !!submit,
        dialog: true,
    };
}"""


def _fill_and_submit_template(page):
    """填写测试模板表单并提交，兼容普通页、弹窗和 iframe。"""
    for frame in reversed(page.frames):
        try:
            result = frame.evaluate(FILL_TEMPLATE_JS, {
                "title": 'Qoder 签到通知',
                "content": notify.TEMPLATE,
            })
            if not (result.get("dialog") and result.get("title")
                    and result.get("content") and result.get("submit")):
                continue
            submit = frame.locator('[data-qoder-template-submit="true"]').first
            try:
                submit.click(timeout=5000)
            except Exception:
                # A timeout may occur after submission; caller reconciles via API.
                return False
            return True
        except Exception:
            continue
    return False



def trusted(frame):
    return urlparse(frame.url).scheme == 'https' and urlparse(frame.url).hostname == 'mp.weixin.qq.com'


def login(context, timeout=300):
    page = context.new_page()
    page.goto(URL, wait_until='domcontentloaded', timeout=45000)
    print('请在浏览器扫码登录微信测试号；无需复制 AppID 或 AppSecret。', flush=True)
    deadline = time.monotonic() + timeout
    clicked = False
    seen_locations = set()
    while time.monotonic() < deadline:
        for candidate in reversed(context.pages):
            if candidate.is_closed():
                continue
            location = urlparse(candidate.url)
            if location.hostname == 'mp.weixin.qq.com' and location.scheme == 'http':
                candidate.goto(candidate.url.replace('http://', 'https://', 1), wait_until='domcontentloaded')
            safe_location = (location.scheme, location.hostname, location.path)
            if location.hostname and safe_location not in seen_locations:
                print('正在识别测试号控制台：' + location.scheme + '://' + (location.hostname or '') + location.path, flush=True)
                seen_locations.add(safe_location)
            for frame in candidate.frames:
                if not trusted(frame):
                    continue
                creds = frame.evaluate(EXTRACT_CREDS_JS)
                if creds.get('appid') and creds.get('appsecret'):
                    candidate.bring_to_front()
                    return candidate, creds['appid'], creds['appsecret']
                button = frame.get_by_text('登录', exact=True).first
                if not clicked and button.count() and button.is_visible():
                    button.click(timeout=5000)
                    clicked = True
        if not context.pages:
            raise notify.NotifyError('浏览器已关闭，原配置未修改')
        context.pages[-1].wait_for_timeout(1000)
    raise notify.NotifyError('等待微信扫码登录超时，原配置未修改')


def choose_template(ids, configured=''):
    return configured if configured in ids else (sorted(ids)[0] if ids else '')


def ensure_template(page, config):
    _, ids = notify.binding_options(config)
    selected = choose_template(ids, config.get('wx_test_template_id', ''))
    if selected:
        print('已找到兼容模板，直接复用。', flush=True)
        return selected
    print('没有兼容模板，正在创建 Qoder 签到通知模板……', flush=True)
    if not _click_new_template(page):
        raise notify.NotifyError('未找到新增测试模板入口，原配置未修改')
    # Submit at most once. Even if the UI times out, check the API before failing.
    _fill_and_submit_template(page)
    for _ in range(20):
        _, ids = notify.binding_options(config)
        if ids:
            print('已通过微信 API 确认模板创建成功。', flush=True)
            return choose_template(ids)
        page.wait_for_timeout(1000)
    raise notify.NotifyError('未能通过 API 确认模板创建；请检查页面后重试，已有模板不会删除')


def select_recipient(before, current, configured=''):
    added = set(current) - set(before)
    if len(added) == 1:
        return next(iter(added))
    if len(added) > 1:
        return ''
    if configured in current:
        return configured
    return current[0] if len(current) == 1 else ''


def ensure_recipient(page, config):
    before, _ = notify.binding_options(config)
    selected = select_recipient(before, before, config.get('wx_test_touser', ''))
    if selected:
        return selected
    if len(before) > 1:
        print('测试号有多个关注者，请选择接收者（仅显示部分标识）：', flush=True)
        for i, value in enumerate(before, 1):
            print(f'{i}. {value[:6]}…{value[-4:]}')
        value = input('接收者序号；回车则等待新关注者：').strip()
        if value:
            if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= len(before):
                raise notify.NotifyError('接收者序号无效，原配置未修改')
            return before[int(value)-1]
    page.bring_to_front()
    label = page.get_by_text('测试号二维码', exact=False).first
    if label.count():
        label.scroll_into_view_if_needed()
    print('请扫码关注页面中的测试号二维码；关注后自动识别，无需填写 OpenID。', flush=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        current, _ = notify.binding_options(config)
        selected = select_recipient(before, current)
        if selected:
            return selected
        page.wait_for_timeout(2500)
    raise notify.NotifyError('未识别到唯一接收者，原配置未修改')


def bind(config):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as runtime:
        context = qoder_auth._launch_login_context(runtime, 'wechat')
        try:
            page, appid, secret = login(context)
            candidate = dict(config)
            # A different test account cannot inherit the old account's recipient.
            if appid != config.get('wx_test_appid'):
                candidate.pop('wx_test_touser', None)
                candidate.pop('wx_test_template_id', None)
            candidate.update(wx_test_appid=appid, wx_test_secret=secret)
            candidate['wx_test_template_id'] = ensure_template(page, candidate)
            candidate['wx_test_touser'] = ensure_recipient(page, candidate)
            candidate['notify_channel'] = 'wx_test'
            notify.validate(candidate)
            return candidate
        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.input.read_text())
        candidate = bind(config)
        q.private_json(args.output, {key: candidate[key] for key in (*notify.FIELDS, 'notify_channel')})
        return 0
    except KeyboardInterrupt:
        return 130
    except notify.NotifyError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('微信浏览器绑定失败；原配置未修改。请确认扫码完成、网络可用及浏览器未被关闭。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
