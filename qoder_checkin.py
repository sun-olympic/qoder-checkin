#!/usr/bin/env python3
"""Qoder daily check-in client. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from datetime import datetime, time as clock_time, timedelta, timezone
from getpass import getpass
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tempfile
from urllib import error, parse, request
from qoder_device import DeviceError, native_headers, validate_headers


ROOT = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))
PREFIX = "/sash/api/v1/me/daily-check-in"
CAMPAIGNS = "/sash/api/v1/me/campaigns"
BASES = {"global": "https://openapi.qoder.sh", "cn": "https://gateway.qoder.com.cn"}
HOSTS = {
    "global": {"openapi.qoder.sh"},
    "cn": {"gateway.qoder.com.cn", "openapi.qoder.com.cn"},
}
UNAVAILABLE = {"NOT_STARTED", "ENDED", "EXPIRED", "NOT_ELIGIBLE", "INELIGIBLE",
               "UNAVAILABLE", "NOT_AVAILABLE", "DISABLED"}
RETRYABLE = {429, 500, 502, 503, 504}
MAX_BODY = 1024 * 1024


class CheckinError(Exception):
    pass


class ConfigError(CheckinError):
    pass


class AuthError(CheckinError):
    pass


class ApiError(CheckinError):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward an account credential to a redirect target.
        return None


@dataclass
class Account:
    name: str
    region: str
    base_url: str
    token_env: str
    token_file: Path | None = None
    api_mode: str = "campaigns"
    auth_source: str = "file"
    expected_account_id: str | None = None
    client_version: str | None = None
    device_source: str = "none"
    qoder_app_path: str | None = None

    def device_headers(self) -> dict:
        if self.device_source == "none":
            return {}
        if not self.expected_account_id:
            raise ConfigError("设备信息模式需要 expected_account_id，请先导入并绑定当前账号")
        try:
            return native_headers(self.region, self.expected_account_id, self.qoder_app_path)
        except DeviceError as exc:
            raise AuthError(str(exc)) from None

    def sync_login(self) -> None:
        if self.auth_source != "macos":
            return
        if sys.platform != "darwin" or self.token_file is None:
            raise ConfigError("macos 登录同步需要 macOS 和 token_file")
        if os.environ.get(self.token_env, "").strip():
            raise ConfigError("macos 同步模式不能同时设置 Token 环境变量，以免使用另一个账号")
        node = shutil.which("node")
        if not node:
            raise ConfigError("同步 Qoder 登录凭证需要 Node.js，请检查 PATH")
        try:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            # Import beside the destination so the final replacement is atomic.
            # Neither failed validation nor cancellation touches the old credential.
            with tempfile.TemporaryDirectory(prefix='.qoder-auth-', dir=self.token_file.parent) as directory:
                candidate = Path(directory) / 'credentials.json'
                result = subprocess.run([node, str(ROOT / 'import_macos_auth.mjs'),
                                         '--region', self.region, '--output', str(candidate)],
                                        capture_output=True, text=True, timeout=190)
                if result.returncode != 0:
                    raise AuthError('同步登录凭证失败；请确认 Qoder 已登录，并完成钥匙串授权')
                data = json.loads(candidate.read_text(encoding='utf-8'))
                if not isinstance(data, dict) or not isinstance(data.get('account_id'), str):
                    raise AuthError('同步凭证格式无效，已保留原凭证')
                if self.expected_account_id and data['account_id'] != self.expected_account_id:
                    raise AuthError('Qoder 当前账号已改变；已停止领取并保留原凭证，请核对绑定账号')
                replace(self, token_file=candidate, token_env='', auth_source='file').token()
                candidate.chmod(0o600)
                os.replace(candidate, self.token_file)
        except (OSError, ValueError, RecursionError, subprocess.TimeoutExpired):
            raise AuthError('同步登录凭证失败或超时；已保留原凭证，请检查登录状态与钥匙串授权') from None

    def token(self) -> str:
        value = os.environ.get(self.token_env, "").strip()
        if not value and self.token_file is not None:
            if not self.token_file.exists():
                raise ConfigError(f"缺少凭证；请设置 {self.token_env} 或执行 init")
            try:
                raw = self.token_file.read_text(encoding="utf-8").strip()
                if raw.startswith("{"):
                    data = json.loads(raw)
                    expires = data.get("expiresAt")
                    if expires is not None:
                        try:
                            expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                            if expiry.tzinfo is None:
                                raise ValueError()
                        except (AttributeError, TypeError, ValueError):
                            raise ConfigError("凭证 expiresAt 格式错误") from None
                        if expiry <= datetime.now(timezone.utc):
                            raise AuthError("登录凭证已过期；请重新登录 Qoder 并同步凭证")
                    if (self.expected_account_id and data.get("account_id") is not None
                            and data["account_id"] != self.expected_account_id):
                        raise AuthError("凭证账号与绑定账号不符，已停止领取")
                    value = data.get("token", data.get("access_token", ""))
                else:
                    value = raw
            except (OSError, ValueError, AttributeError, RecursionError):
                raise ConfigError("无法读取凭证文件；请检查文件路径及 JSON 格式") from None
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"缺少凭证；请设置 {self.token_env} 或执行 init")
        value = value.strip()
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        if not value or len(value) > 16384 or any(not 33 <= ord(c) <= 126 for c in value):
            raise ConfigError("Token 格式错误；请只填写 Authorization 中的 Bearer 凭证")
        return value


def default_accounts() -> list[dict]:
    return [{"name": region, "region": region, "token_env": f"QODER_{region.upper()}_TOKEN",
             "token_file": f"credentials-{region}.json"} for region in BASES]


def load_accounts(path: Path, region: str) -> list[Account]:
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            raise ConfigError("配置文件不可读或不是合法 JSON") from None
        if not isinstance(config, dict):
            raise ConfigError("配置文件必须是 JSON 对象")
        rows = config.get("accounts")
    else:
        rows = default_accounts()
    if not isinstance(rows, list) or not rows:
        raise ConfigError("accounts 必须是非空数组")
    accounts, names = [], set()
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("region"), str) or
                row["region"] not in BASES):
            raise ConfigError("每个账号的 region 必须为 global 或 cn")
        kind = row["region"]
        name = row.get("name", kind)
        if not isinstance(name, str) or not re.fullmatch(r"[\w.-]{1,64}", name) or name in names:
            raise ConfigError("账号 name 必须唯一，且只含 1–64 个字母、数字、下划线、点或连字符")
        names.add(name)
        base = row.get("base_url", BASES[kind])
        if not isinstance(base, str):
            raise ConfigError("base_url 必须是字符串")
        try:
            url = parse.urlsplit(base)
        except ValueError:
            raise ConfigError("base_url 不是合法 URL") from None
        if (url.scheme != "https" or url.netloc not in HOSTS[kind] or
                url.path not in ("", "/") or url.query or url.fragment):
            raise ConfigError("base_url 必须是对应地区的 Qoder HTTPS 网关根地址")
        env = row.get("token_env", f"QODER_{kind.upper()}_TOKEN")
        if not isinstance(env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
            raise ConfigError("token_env 必须是有效的环境变量名")
        file = row.get("token_file")
        if file is not None and (not isinstance(file, str) or not file):
            raise ConfigError("token_file 必须是非空路径")
        token_file = (path.parent / Path(file).expanduser()).resolve() if file else None
        mode = row.get("api_mode", "campaigns")
        source = row.get("auth_source", "file")
        expected = row.get("expected_account_id")
        version = row.get("client_version")
        device_source = row.get("device_source", "none")
        app_path = row.get("qoder_app_path")
        if device_source not in ("none", "qoder"):
            raise ConfigError("device_source 应为 none/qoder")
        if app_path is not None and (not isinstance(app_path,str) or not app_path):
            raise ConfigError("qoder_app_path 必须为非空路径")
        if mode not in ("campaigns", "legacy") or source not in ("file", "macos"):
            raise ConfigError("api_mode 应为 campaigns/legacy，auth_source 应为 file/macos")
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[\w-]{1,128}", expected)):
            raise ConfigError("expected_account_id 格式错误")
        if version is not None and (not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", version)):
            raise ConfigError("client_version 格式错误")
        if region in ("both", kind):
            accounts.append(Account(name, kind, base.rstrip("/"), env, token_file,
                                    mode, source, expected, version, device_source, app_path))
    if not accounts:
        raise ConfigError("配置中没有所选地区的账号")
    return accounts


@dataclass
class Response:
    status: int
    body: dict
    headers: dict = field(default_factory=dict)


class Transport:
    def __init__(self, base_url: str, token: str, timeout: float = 20, client_version: str | None = None, device_headers: dict | None = None):
        try:
            self.device_headers = validate_headers(device_headers or {})
        except DeviceError as exc:
            raise ConfigError(str(exc)) from None
        self.base_url, self._token, self.timeout = base_url, token, timeout
        self.client_version = client_version
        self.opener = request.build_opener(NoRedirect())

    def send(self, method: str, path: str, *, empty_body: bool = False) -> Response:
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            req = request.Request(self.base_url + path,
                                  data=(b"" if empty_body else b"{}") if method == "POST" else None,
                                  method=method,
                                  headers={"Authorization": "Bearer " + self._token,
                                           "Accept": "application/json",
                                           "Content-Type": "application/json",
                                           "User-Agent": "Qoder",
                                           "Cosy-ClientType": "10",
                                           **({"Cosy-Version": self.client_version} if self.client_version else {}),
                                           **self.device_headers})
            try:
                try:
                    response = self.opener.open(req, timeout=self.timeout)
                except error.HTTPError as exc:
                    response = exc
                with response:
                    status, headers = response.code, dict(response.headers)
                    raw = response.read(MAX_BODY + 1)
            except (OSError, error.URLError, HTTPException):
                if attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError("网络请求失败或超时；请检查网络、代理和证书") from None
            if status in RETRYABLE and attempt + 1 < attempts:
                delay = 2 ** attempt
                retry_after = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
                retry_after = str(retry_after).strip()
                if re.fullmatch(r'[0-9]+', retry_after):
                    digits = retry_after.lstrip('0') or '0'
                    seconds = 30 if len(digits) > 2 else min(30, int(digits))
                    delay = max(delay, seconds)
                time.sleep(delay)
                continue
            if status in (401, 403):
                raise AuthError(f"HTTP {status}：凭证失效或账号无访问权限，请重新登录 Qoder 并更新凭证")
            if 300 <= status < 400:
                raise ApiError("接口发生重定向，已停止请求；请核对网关地址")
            if status in (404, 405, 410):
                raise ApiError(f"HTTP {status}：签到接口不可用，请核对该版本的网关及活动是否仍开放")
            if len(raw) > MAX_BODY:
                raise ApiError("接口响应过大，已停止解析")
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ApiError(f"HTTP {status}：接口未返回 JSON") from None
            if not isinstance(body, dict):
                raise ApiError("接口返回了未知格式，未判定为签到成功")
            return Response(status, body, headers)
        raise AssertionError("unreachable")


def has_error(body: dict) -> bool:
    code = body.get("code")
    return (body.get("success") is False or bool(body.get("error")) or
            bool(body.get("errorCode")) or
            (code is not None and (isinstance(code, bool) or code not in (0, 200, "0", "200", "OK", "SUCCESS"))))


def payload(response: Response) -> dict:
    outer = response.body
    if not 200 <= response.status < 300 or has_error(outer):
        raise ApiError(f"接口拒绝请求（HTTP {response.status}）；请在 Qoder 中查看活动或账号状态")
    data = outer.get("data", outer)
    if not isinstance(data, dict) or has_error(data):
        raise ApiError("接口返回业务错误或未知数据格式")
    return data


def details(data: dict) -> dict:
    # Only report public reward metadata. Never echo tokens, headers or raw responses.
    result = {}
    for key in ("rewardCredits", "nextClaimAt", "currentStreakDays", "totalClaimDays",
                "totalRewardCredits", "lastClaimedAt", "rewardExpiresAt"):
        val = data.get(key)
        if type(val) in (int, float) and abs(val) <= 10**18 and math.isfinite(val):
            result[key] = val
    return result


class Client:
    """Legacy QoderWork endpoint, available only through explicit configuration."""
    def __init__(self, transport: Transport, expected_account_id: str | None = None):
        self.transport = transport
        self.expected_account_id = expected_account_id

    def status(self) -> dict:
        data = payload(self.transport.send("GET", PREFIX + "/status"))
        if self.expected_account_id and data.get('uid') != self.expected_account_id:
            raise AuthError('旧接口未确认绑定账号，已停止领取；请核对账号或改用 campaigns 协议')
        state = data.get("status")
        if state == "CLAIMED":
            return {"result": "already_claimed", "message": "本轮已领取", **details(data)}
        if state == "CLAIMABLE":
            return {"result": "claimable", "message": "本轮可领取", **details(data)}
        if isinstance(state, str) and state in UNAVAILABLE:
            return {"result": "unavailable", "message": "当前不可领取，请在 Qoder 中确认活动资格或时间",
                    "server_status": state, **details(data)}
        raise ApiError("未知签到状态；未提交领取请求，请核对接口响应格式")

    def claim(self) -> dict:
        before = self.status()
        if before["result"] != "claimable":
            return before
        try:
            response = self.transport.send("POST", PREFIX + "/claim")
            body = response.body
            inner = body.get("data", body)
            if isinstance(inner, dict) and response.status in (200, 409):
                if (inner.get("result") == "ALREADY_CLAIMED" or
                        (response.status == 409 and inner.get("errorCode") == "AlreadyExists")):
                    return {"result": "already_claimed", "message": "本轮已领取", **details(inner)}
            data = payload(response)
            if data.get("success") is True:
                return {"result": "claimed", "message": "签到成功", **details(data)}
            reason = "领取响应缺少明确的成功标记"
        except AuthError:
            raise
        except ApiError as exc:
            reason = str(exc)
        # POST is never blindly retried: timeout may mean it was already committed.
        try:
            after = self.status()
            if after["result"] == "already_claimed":
                after["message"] = "复查确认本轮已领取"
                after["verified_after_claim"] = True
                return after
        except CheckinError:
            pass
        raise ApiError(reason + "；复查未能确认到账，可稍后重新运行（会先查状态）")


class CampaignClient:
    """Current desktop protocol; discover daily campaigns instead of hardcoding IDs."""

    def __init__(self, transport: Transport, expected_account_id: str | None = None):
        self.transport = transport
        self.expected_account_id = expected_account_id

    def campaigns(self) -> list[dict]:
        data = payload(self.transport.send("GET", CAMPAIGNS))
        if self.expected_account_id and data.get("uid") != self.expected_account_id:
            raise AuthError("接口账号与绑定账号不一致，已停止领取")
        rows = data.get("campaigns")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ApiError("活动列表格式未知；未提交领取请求")
        if data.get("showCampaign") is False:
            return []
        return rows

    @staticmethod
    def is_daily(campaign: dict) -> bool:
        if campaign.get("actionType") != "CLAIM_BENEFIT":
            return False
        benefit = campaign.get("benefit")
        if not isinstance(benefit, dict) or benefit.get("kind") != "CREDITS":
            return False
        # Avoid claiming subscription promotions or unrelated model-specific bonuses.
        texts = []
        placements = campaign.get("placements", [])
        if not isinstance(placements, list):
            return False
        for placement in placements:
            if not isinstance(placement, dict):
                continue
            content = placement.get("content", {})
            if not isinstance(content, dict):
                continue
            for language in content.values():
                if isinstance(language, dict):
                    texts.extend(value for key, value in language.items()
                                 if key in ("title", "description") and isinstance(value, str))
        text = " ".join(texts).lower()
        return (any(word in text for word in ("每天", "每日", "daily", "every day")) and
                ("credit" in text or "积分" in text))

    @staticmethod
    def summarize(campaign: dict) -> dict:
        state = campaign.get("claimStatus")
        identity = campaign.get("campaignId")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
            raise ApiError("活动 ID 格式未知，已停止领取")
        amount = campaign.get("benefit", {}).get("amount")
        metadata = {"campaign_id": identity, **details({"rewardCredits": amount})}
        if state in ("CLAIMED", "CLAIMABLE"):
            now = time.time()
            start, end = campaign.get("startAt"), campaign.get("endAt")
            if (type(start) not in (int, float) or type(end) not in (int, float) or
                    not 0 <= start < end <= 253402300799 or
                    not math.isfinite(start) or not math.isfinite(end)):
                raise ApiError("活动领取窗口未知，已停止领取")
            if start <= now < end:
                if state == "CLAIMED":
                    return {"result": "already_claimed", "message": "本轮每日奖励已领取", **metadata}
                return {"result": "claimable", "message": "本轮每日奖励可领取", **metadata}
            return {"result": "unavailable", "message": "当前不在活动领取窗口内", **metadata}
        if isinstance(state, str) and state in UNAVAILABLE | {"NOT_CLAIMABLE"}:
            return {"result": "unavailable", "message": "当前每日奖励不可领取",
                    "server_status": state, **metadata}
        raise ApiError("每日奖励状态未知，未提交领取请求")

    def select(self, rows: list[dict]) -> dict | None:
        candidates = [row for row in rows if self.is_daily(row)]
        if not candidates:
            return None
        available = [row for row in candidates if self.summarize(row)["result"] == "claimable"]
        if len(available) > 1:
            raise ApiError("发现多个可领取的每日活动，需核对后再领取")
        if available:
            return available[0]
        claimed = [row for row in candidates if self.summarize(row)["result"] == "already_claimed"]
        return (claimed or candidates)[0]

    def status(self) -> dict:
        selected = self.select(self.campaigns())
        if selected is None:
            return {"result": "unavailable", "message": "本次请求未获得每日 Credits 活动；若客户端可见，请检查设备信息配置",
                    "server_status": "NO_DAILY_CAMPAIGN"}
        return self.summarize(selected)

    def claim(self) -> dict:
        before = self.status()
        if before["result"] != "claimable":
            return before
        identity = before["campaign_id"]
        acknowledged = False
        reason = "领取响应未明确确认成功"
        try:
            data = payload(self.transport.send("POST", f"{CAMPAIGNS}/{identity}/claim", empty_body=True))
            acknowledged = data.get("status") == "CLAIMED"
        except AuthError:
            raise
        except ApiError as exc:
            reason = str(exc)
        # Reconcile the same ID, not tomorrow's campaign or an unrelated promotion.
        try:
            match = next((row for row in self.campaigns() if row.get("campaignId") == identity), None)
            if match and match.get("claimStatus") == "CLAIMED":
                return {**before, "result": "claimed", "message": "每日奖励领取成功，复查确认已领取",
                        "verified_after_claim": True}
        except CheckinError:
            if acknowledged:
                return {**before, "result": "claimed", "message": "接口已确认领取成功，状态复查暂不可用",
                        "verified_after_claim": False}
        if acknowledged:
            return {**before, "result": "claimed", "message": "接口已确认领取成功，活动列表尚未更新",
                    "verified_after_claim": False}
        raise ApiError(reason + "；复查未确认本活动已领取，未重复提交")


def run_once(config: Path, region: str, action: str, timeout: float) -> list[dict]:
    accounts = load_accounts(config, region)
    results = []
    for account in accounts:
        try:
            account.sync_login()
            transport = Transport(account.base_url, account.token(), timeout, account.client_version, account.device_headers())
            client = (CampaignClient(transport, account.expected_account_id)
                      if account.api_mode == "campaigns" else Client(transport, account.expected_account_id))
            item = client.status() if action == "status" else client.claim()
        except CheckinError as exc:
            item = {"result": "error", "message": str(exc), "error_type": type(exc).__name__}
        results.append({"account": account.name, "region": account.region, **item})
    return results


def emit(results: list[dict], as_json: bool) -> None:
    now = datetime.now(BEIJING).isoformat(timespec="seconds")
    if as_json:
        print(json.dumps({"time": now, "accounts": results}, ensure_ascii=False), flush=True)
    else:
        for item in results:
            credits = f"，奖励 {item['rewardCredits']} Credits" if "rewardCredits" in item else ""
            print(f"{now} [{item['account']}/{item['region']}] {item['message']}{credits}", flush=True)


def exit_code(results: list[dict]) -> int:
    if any(item["result"] == "error" for item in results):
        return 1
    return 3 if any(item["result"] == "unavailable" for item in results) else 0


def next_run(now: datetime, at: clock_time) -> datetime:
    now = now.astimezone(BEIJING)
    target = datetime.combine(now.date(), at, tzinfo=BEIJING)
    return target + timedelta(days=1) if target <= now else target


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids replacing existing credentials or following a symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def initialize(config: Path, region: str) -> None:
    kinds = list(BASES) if region == "both" else [region]
    if config.exists():
        raise ConfigError("配置文件已存在；请直接编辑配置或选用新的 --config 路径")
    rows = [row for row in default_accounts() if row["region"] in kinds]
    for row in rows:
        if (config.parent / row["token_file"]).exists():
            raise ConfigError("凭证文件已存在；为避免覆盖，请使用原文件或新的配置目录")
    tokens = {}
    for row in rows:
        val = getpass(f"请输入 {row['region']} 的 Bearer Token（输入隐藏；留空则使用环境变量）：").strip()
        if val:
            tokens[row["region"]] = val
    for row in rows:
        if row["region"] in tokens:
            private_json(config.parent / row["token_file"], {"token": tokens[row["region"]]})
    private_json(config, {"accounts": rows})
    print("配置已生成。运行 status 查询状态，运行 claim 执行签到。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Qoder 国际版 / 国内版每日签到（Python 标准库）")
    parser.add_argument("command", choices=["init", "status", "claim", "daemon"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json", help="配置文件路径")
    parser.add_argument("--region", choices=["global", "cn", "both"], default="both")
    parser.add_argument("--timeout", type=float, default=20, help="单次 HTTP 超时秒数，默认 20")
    parser.add_argument("--json", action="store_true", help="输出 JSON，适合定时任务日志")
    parser.add_argument("--at", default="10:05", help="daemon 每日运行时间，UTC+8，默认 10:05")
    args = parser.parse_args(argv)
    try:
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 120:
            raise ConfigError("timeout 必须在 0–120 秒之间")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", args.at):
            raise ConfigError("at 必须为 HH:MM，例如 10:05")
        at = clock_time.fromisoformat(args.at)
        config = args.config.expanduser().resolve()
        if not config.exists() and config != ROOT / "config.json":
            if args.command != "init":
                raise ConfigError("指定的配置文件不存在")
        if args.command == "init":
            initialize(config, args.region)
            return 0
        while True:
            results = run_once(config, args.region, args.command, args.timeout)
            emit(results, args.json)
            if args.command != "daemon":
                return exit_code(results)
            target = next_run(datetime.now(BEIJING), at)
            while True:
                remaining = (target - datetime.now(BEIJING)).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 60))
    except (CheckinError, OSError) as exc:
        message = str(exc) if isinstance(exc, CheckinError) else "无法访问本地配置或凭证文件"
        emit([{"account": "config", "region": args.region, "result": "error", "message": message}], args.json)
        return 2
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == "__main__":
    sys.exit(main())
