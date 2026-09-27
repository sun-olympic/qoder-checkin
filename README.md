# Qoder 每日 Credits 自动领取

通过 Qoder HTTP 接口发现每日活动、领取 Credits 并复查结果。Python 3.10+，Python 部分仅用标准库。支持不同地区和多个账号的独立凭证，不需要启动或保持 Qoder 窗口打开。当前国际版每日活动需要本机设备信息，脚本直接调用已安装 Qoder 的独立组件获取。

## 快速安装

先检查 Python：macOS / Linux 使用 `python3 --version`，Windows 使用 `python --version`。低于 3.10 时请安装 [Python 官方版本](https://www.python.org/downloads/)。下载项目后，在项目目录执行：

```bash
# macOS（Windows 将 python3 换成 python）
python3 checkin_cli.py wizard
python3 checkin_cli.py doctor
python3 checkin_cli.py install
python3 checkin_cli.py status
```

`wizard` 负责选择地区、配置 Token 和运行时间；已有配置会保留账号与凭证，只调整时间。`doctor` 检查本地凭证格式和依赖，不调用接口，也不会弹出钥匙串。`install` 注册系统任务，`status` 必须显示“已注册”才代表系统调度生效。

首次注册会在 macOS 立即补跑；Windows 在每日时间或下次登录时运行。可手动执行 `run` 验证。**原生任务的时间采用电脑本地时区**，默认 10:05；Qoder 每日活动以服务端窗口为准，本机为 UTC+8。

```bash
python3 checkin_cli.py run --dry-run     # 只查询，不领取
python3 checkin_cli.py run               # 立即领取，已领取则跳过
python3 checkin_cli.py install --at 10:10 # 改时间并重新注册
python3 checkin_cli.py uninstall         # 卸载任务，保留凭证、配置、日志
```

移动项目时请连同 `config.json.schedule-id` 一起移动，再执行 `install`；该标识通过完整写入后原子发布，用于替换原任务，避免留下旧任务。旧版本升级后须先在原目录执行一次 `install` 生成标识，再移动。复制目录及标识表示迁移同一任务；若要独立部署另一任务，请在新目录删除复制的标识后安装。更换 Python、修改时间或执行卸载后也需重新 `install`。修改账号或更新凭证不需要重装。不要同时运行旧 `daemon`、系统任务和 CI，避免多个调度来源。

## 支持范围

| 能力 | macOS | Windows | Linux / CI |
| --- | --- | --- | --- |
| Token 文件 / 环境变量调用接口 | 支持，本机国际版已验证 | 已实现，待真机验证 | 支持标准库运行；CI 工作流已提供 |
| 原生每日任务 + 登录补跑 | launchd，已注册并有后台执行记录 | 任务计划，已验证 XML/命令生成，待真机验证 | 无原生安装器，可用 daemon / CI |
| 不启动 IDE 领取 | 已实测；需保留已安装的官方组件 | 设备组件未适配，待验证 | 仅 Token 模式；当前每日活动不保证可用 |
| 自动导入客户端凭证 | 国际版 Qoder App 已验证，需钥匙串授权 | 未实现 | 未实现 |
| 国内版领取 | 网关和独立凭证配置已实现，尚无账号实测 | 同左 | 同左 |
| 多账号 | accounts 数组；每个账号独立凭证，失败互不阻断 | 同左 | 同左 |

仅 Token 模式不保证能获得完整活动列表：本机实测同一账号缺少设备信息时只收到订阅活动，补全后才收到每日 100 Credits。本项目没有实现 Qoder 独立扫码登录或自动刷新会话，不能复用 WorkBuddy / CodeBuddy 的认证流程。历史 IDE 版本的凭证存储不保证与当前 Qoder App 相同。

## 登录与凭证

向导输入的 Token 不回显。也可复制 `config.example.json` 创建 `config.json`，分别创建 `credentials-global.json` / `credentials-cn.json`：

```json
{"token":"该地区客户端的 Bearer Token"}
```

支持 `access_token` 字段和纯文本 Token。环境变量 `QODER_GLOBAL_TOKEN` / `QODER_CN_TOKEN` 优先于文件，适合 CI 和手动调用；原生安装器要求有效凭证文件，因为终端环境变量不会自动传给计划任务。没有配置文件时，底层 `qoder_checkin.py` 可直接使用环境变量；管理命令需要先创建配置。

macOS 已安装并登录当前 Qoder App 时，可导入自己的凭证（需要 Node.js）：

```bash
node import_macos_auth.mjs --region global
# 国内版需安装对应 App，尚未实测其认证文件格式
node import_macos_auth.mjs --region cn
```

系统钥匙串授权需要本人完成。配置为 `auth_source: "macos"` 的同步会先导入临时文件，通过绑定账号和凭证格式校验后才以 0600 权限原子替换正式文件；账号不符、导入失败或取消均保留原凭证，不输出 Token。独立的 Node 导入命令是显式导出当前登录账号，会覆盖其指定输出文件。新建的凭证文件可由向导复用；若需固定账号，在配置中填写 `expected_account_id`（来自凭证文件的 `account_id`），不要将其设置为另一个账号。当前本机配置已绑定。

凭证为明文，仅用于指定地区的 Qoder HTTPS 网关，禁止跟随重定向。默认文件名已被 Git 忽略；Windows 上 `chmod` 不等价于 NTFS ACL，请将凭证存放在自己的用户目录。凭证到期、账号不匹配或 HTTP 401/403 时停止该账号，不会回退到旧账号。重新登录 Qoder 后再次导入即可恢复。

`auth_source: "macos"` 可用于手动运行时自动同步，但原生后台安装器要求 `file` 模式，避免无人值守时卡在授权提示。没有安装 Node.js 不影响普通 Token 模式。

## 多账号配置

`accounts` 数组允许同一地区多个账号。每项 `name` 必须唯一，并使用不同 `token_file` 和 `token_env`，否则可能重复读取同一凭证。例如：

```json
{
  "schedule": {"at": "10:05"},
  "accounts": [
    {"name":"global-alice","region":"global","token_file":"credentials-alice.json","token_env":"QODER_ALICE_TOKEN"},
    {"name":"global-bob","region":"global","token_file":"credentials-bob.json","token_env":"QODER_BOB_TOKEN"},
    {"name":"cn","region":"cn","token_file":"credentials-cn.json","token_env":"QODER_CN_TOKEN"}
  ]
}
```

`--region global|cn|both` 选择地区。`install` 未指定地区时沿用已保存的 `schedule.region`，首次安装默认 both；其他命令仍默认 both。配置向导修改时间会保留原地区范围。`install --region global` 注册的任务只运行国际版；若改变任务的地区范围需重新安装。账号增删改名通过编辑配置完成，暂未实现独立账户管理命令。

当前本机已启用以下配置（不需要重装定时任务）：

```json
{"device_source":"qoder","expected_account_id":"填写凭证文件中的 account_id"}
```

`device_source: "qoder"` 在每次运行时调用 `/Applications/Qoder.app/Contents/Resources/umid/runtime-info`，读取本机既有 `auth.machine-id` 并携带官方生成的设备信息。它不启动 Electron / IDE，不伪造设备标识，也不把设备 Token 输出到日志。需要已安装并完成首次登录的 Qoder；可用 `qoder_app_path` 指定应用路径。设备组件失败会停止本次请求，不静默退回缺少设备信息的请求。国内版可配置对应应用路径，但尚未实测；Windows 设备组件尚未适配。

`device_source: "none"` 是兼容默认值，适用于不要求设备信息的接口，不能把该模式的空活动列表理解为客户端也没有活动。

其他配置：`api_mode: "campaigns"` 为默认新协议；`legacy` 保留旧 `/daily-check-in` 协议，需确认对应服务端支持。配置 `expected_account_id` 后，新旧协议都必须由服务端返回匹配的 `uid` 才能领取；旧接口若不提供 `uid` 会停止，建议使用 campaigns 协议。`client_version` 仅接受 ASCII 字母、数字、点、下划线和连字符，用于客户端版本头。`base_url` 只能是该地区允许的官方网关。

## 调度与诊断

macOS 使用用户 LaunchAgent，每日定时运行、登录时补跑；睡眠期间错过的日历任务由 launchd 在唤醒后处理。Windows 使用单一用户任务，包含每日与当前用户登录两个触发器，配置错过时间后尽快执行，优先使用 `pythonw.exe` 隐藏控制台。这些任务不需要管理员账号密码；权限拒绝时请检查本机任务策略。任务只在用户登录会话中运行，不会唤醒关机电脑。

同一配置的运行命令使用内核文件锁防止重叠执行，进程退出后自动释放。锁名保留完整配置文件名，扩展名不同的配置互不阻塞。wizard / install / uninstall 另用管理锁，防止并发覆盖配置或任务。Windows 加锁前不读写锁定字节，竞争时跳过；其他文件或句柄错误会明确报错。接口本身仍以服务端状态防重；不同配置/不同电脑不能共享本地锁。常驻旧命令不使用此锁，请不要与原生任务混跑。

`uninstall` 不要求配置文件存在或内容有效，使用同一 `--config` 路径即可；请保留任务标识文件。macOS 安装在停旧任务前准备文件，后续失败时恢复旧定义并重新加载；Windows 更新前也会备份旧任务 XML，失败时恢复旧任务或清理首次创建的任务。配置先原子保存，写入失败不会修改系统任务；任务安装失败时恢复原配置。安装期间按 Ctrl+C 也会先恢复任务与配置，再返回退出码 130；回滚失败会明确报错并保留可用恢复文件。任务查询只有明确返回“不存在”时才视为未注册，权限错误或未知错误会中止操作，不会误报卸载成功。

`checkin.log` 在读取配置前初始化，记录启动错误和筛选后的结果；项目目录不可写，或运行中主日志写入、轮换失败时，会将同一条记录写入 `~/.qoder-checkin/logs/<配置路径摘要>.log`，不会重试领取接口；两个日志位置都失败时返回非零退出码。错误日志不记录原始异常内容或凭证。单文件约 1 MiB，保留 3 份备份。macOS 的 `scheduler.log` 用于启动器排错。`status` 反映注册状态，领取结果以 `checkin.log` 为准。系统升级或移动路径后可重复安装刷新任务。若 macOS 长时间显示 running、日志为空，查看是否有 Python 请求访问“文稿”等项目目录的系统提示，并由本人完成授权；注册成功不代表首次运行成功。

其他调度方式保留：

```bash
# 固定 UTC+8，每天 10:05；退出或重启后需重新启动
python3 qoder_checkin.py daemon --region global --at 10:05 --json
```

`.github/workflows/checkin.yml` 每日 UTC 02:05 执行，可手动触发；在自己的仓库配置对应 Token Secrets。任务上限为 15 分钟，覆盖两区串行查询、领取和状态复查的有界重试。GitHub 调度可能延迟，凭证失效后需更新 Secrets。项目仓库：[sun-olympic/qoder-checkin](https://github.com/sun-olympic/qoder-checkin)。

## 协议与验证

默认国际版 `https://openapi.qoder.sh`，国内版 `https://gateway.qoder.com.cn`（另允许 `https://openapi.qoder.com.cn`）。

1. `GET /sash/api/v1/me/campaigns` 动态获取活动和当前账号。
2. 筛选每日 Credits、`CLAIM_BENEFIT`、处于领取窗口且 `CLAIMABLE` 的唯一活动。
3. 空请求体 `POST /sash/api/v1/me/campaigns/{campaignId}/claim`。
4. 复查同一 ID，已领取时不重复 POST；跳过订阅及其他推广活动。

GET 对临时错误最多尝试三次；POST 不重试，超时先复查，不明确就报错。活动不存在、无资格、未知状态均不伪报成功。接口来自官方客户端活动页的实际请求，属于内部接口，后续可能变化。

```bash
python3 -m unittest discover -s tests -v
node --check import_macos_auth.mjs
```

115 项测试覆盖协议、账号隔离、过期凭证、动态 ID、时间窗口、重试、并发锁、安装回滚、Windows 任务 XML 和微信通知。本地 macOS、Python 3.14 测试通过；另提供三平台、Python 3.10/3.12 的 CI 测试矩阵，不能替代 Windows 真机验证。

2026-09-25 国际版页面实测领取 100 Credits；随后脚本直连接口确认 `already_claimed`。随后已正常退出 Qoder，在 IDE 关闭状态下由 Python 直接 POST 领取新一轮 100 Credits，并通过 GET 复查确认 CLAIMED；领取后 Qoder 仍未启动。国内版尚无有效会话实测。

参考项目：[sun-olympic/workbuddy-checkin](https://github.com/sun-olympic/workbuddy-checkin)，本次参考提交 `98851184d57bd1c003015f4954dd243cd2120cc5`。独立实现 Qoder 的安装管理，未修改参考项目或引入其依赖。

最近一次复审及验证范围见 [REVIEW.md](REVIEW.md)。


## 微信通知（微信测试号）

参照 workbuddy-checkin 的微信测试号方式，支持个人微信模板消息。
执行 `python3 checkin_cli.py wizard`，设置时间后选择「2 配置微信测试号」，即可在主向导内完成通知配置。也可执行 `python3 checkin_cli.py wx-setup` 单独配置通知。扫码登录微信公众平台测试号、关注测试号并创建模板后，在本机终端输入 AppID、AppSecret、接收者 OpenID 和模板 ID（均不回显）。无需把密钥发到聊天中。

模板内容与参考项目一致：
```
结果：{{keyword1.DATA}}
说明：{{keyword2.DATA}}
时间：{{keyword3.DATA}}
```

执行 `python3 checkin_cli.py test-notify` 发送测试通知；接口受理不代表手机一定收到，请在微信确认。
配置字段沿用参考项目：`notify_channel: "wx_test"`、`wx_test_appid`、`wx_test_secret`、`wx_test_touser`、`wx_test_template_id`。设置 `notify_channel: "none"` 可关闭。配置文件权限为 0600；不提交密钥到 Git。

本机定时任务使用 `checkin_cli.py run`，签到结果会自动通知，保存配置后无需重装定时任务。`--dry-run` 不推送；直接运行底层 `qoder_checkin.py` 不推送。通知包含账号名称、地区、结果及积分，不包含 Qoder Token、设备信息或原始异常。每天以北京时间 10:00 为界，同一账号同一活动相同结果仅推送一次；领取成功与已领取合并去重。失败后恢复成功会再次通知。去重记录按配置独立保存，切换接收者可重新通知。

网络请求超时或微信拒绝会记录通知失败并返回非零退出码，不重做领取，也不自动重发不确定的消息；下次计划运行会再尝试。接口受理后才保存去重状态，因此网络中断或保存失败时无法保证严格一次送达。当前接入微信测试号，不含 pushplus、通用 webhook 或参考项目的浏览器自动绑定功能。


微信绑定向导已补充与 workbuddy 类似的 API 辅助流程：填写 AppID/AppSecret 后，选择自动查找，读取已关注用户和与上述内容完全匹配的模板。唯一候选自动选用；多个关注者须明确选择，已有接收者仍在列表中时保留。未找到时可手填 OpenID 或模板 ID，也可直接选择全手动模式。请先在测试号网页完成扫码关注和创建模板，再按回车查询；向导不自动操作网页或创建模板。保存后可选择立即发送测试通知；测试失败仍保留配置，可用 test-notify 重试。
