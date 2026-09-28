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

`wizard` 负责选择地区、配置 Token 和运行时间；已有配置会保留账号与凭证，账号管理步骤输入 `a` 可连续添加账号，直接回车继续时间和微信设置。新增账号后请重新执行 `install` 更新后台任务。`doctor` 检查本地凭证格式和依赖，不调用接口，也不会弹出钥匙串。`install` 注册系统任务，`status` 必须显示“已注册”才代表系统调度生效。

首次注册会在 macOS 立即补跑；Windows 在每日时间或下次登录时运行。可手动执行 `run` 验证。**原生任务的时间采用电脑本地时区**，默认 10:05；Qoder 每日活动以服务端窗口为准，本机为 UTC+8。

```bash
python3 checkin_cli.py run --dry-run     # 只查询，不领取
python3 checkin_cli.py run               # 立即领取，已领取则跳过
python3 checkin_cli.py install --at 10:10 # 改时间并重新注册
python3 checkin_cli.py uninstall         # 卸载任务，保留凭证、配置、日志
python3 checkin_cli.py delete-config     # 仅删除配置，保留凭证、登录缓存、任务标识和日志
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

活动可见性以服务端为准。2026-09-28 使用官方 CLI 登录导入的 Token，在不附加 IDE 设备信息的只读查询中返回了每日活动及已领取状态；此结果不代表所有账号、地区均相同。导出的凭证不会自动刷新，过期需重新授权。历史 IDE 版本的凭证存储不保证与当前 Qoder App 相同。

## 登录与凭证

无需安装 Qoder IDE，可通过官方 CLI 在系统普通浏览器中授权，再导入凭证。当前固定适配 CLI 1.1.64，已验证 macOS Apple Silicon 国际版，需要 Node.js；其他平台和国内版暂用手动 Token。

```bash
python3 checkin_cli.py login --login-region global
python3 checkin_cli.py wizard
```

首次自动下载校验官方 CLI 到 `.qoder-cli-runtime`，通过普通浏览器授权；Qoder 登录不需要 Playwright 或虚拟环境。有本项目登录态时优先导入。导入器使用该固定版本内置的凭证解码组件，属于版本相关适配，并非稳定公开导出 API。导入后先通过只读活动接口核对账号，再以 0600 权限原子保存；失败保留原凭证，Token 不输出到终端或日志。运行时目录与凭证均被 Git 忽略。微信浏览器绑定仍独立使用 Playwright。

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

`delete-config` 仅永久删除 `--config` 指定的配置文件（默认项目内 `config.json`），不放入废纸篓，不清除凭证或登录状态；配置不存在时重复执行仍成功，配置损坏也可删除。macOS / Windows 原生任务仍注册或状态查询失败时拒绝删除，请先对同一配置执行 `uninstall`。其他平台的 cron/CI 等外部调度需自行停止。运行中的签到或配置管理操作也会阻止删除；之后可运行 `wizard` 重建配置。

同一配置的运行命令使用内核文件锁防止重叠执行，进程退出后自动释放。锁名保留完整配置文件名，扩展名不同的配置互不阻塞。wizard / install / uninstall / delete-config 另用管理锁，防止并发覆盖配置或任务。Windows 加锁前不读写锁定字节，竞争时跳过；其他文件或句柄错误会明确报错。接口本身仍以服务端状态防重；不同配置/不同电脑不能共享本地锁。常驻旧命令不使用此锁，请不要与原生任务混跑。

`uninstall` 不要求配置文件存在或内容有效，使用同一 `--config` 路径即可；请保留任务标识文件。macOS 安装在停旧任务前准备文件，后续失败时恢复旧定义并重新加载；Windows 更新前也会备份旧任务 XML，失败时恢复旧任务或清理首次创建的任务。配置先原子保存，写入失败不会修改系统任务；任务安装失败时恢复原配置。安装期间按 Ctrl+C 也会先恢复任务与配置，再返回退出码 130；回滚失败会明确报错并保留可用恢复文件。任务查询只有明确返回“不存在”时才视为未注册，权限错误或未知错误会中止操作，不会误报卸载成功。

`checkin.log` 在读取配置前初始化，记录启动错误和筛选后的结果；项目目录不可写，或运行中主日志写入、轮换失败时，会将同一条记录写入 `~/.qoder-checkin/logs/<配置路径摘要>.log`，不会重试领取接口；两个日志位置都失败时返回非零退出码。错误日志不记录原始异常内容或凭证。单文件约 1 MiB，保留 3 份备份。macOS 启动日志使用 `~/Library/Logs/qoder-checkin/<任务标识>.log`，工作目录使用用户主目录，避免 launchd 在 Python 启动前访问受保护的 Documents；原项目内 `scheduler.log` 保留，不再作为新任务输出。

交互式 `wizard` 保存后及每次 `install` 注册前，会创建临时 LaunchAgent 实测代码导入、配置及凭证读取、签到日志打开；不领取、不推送，结束后卸载检测任务。后台目录访问被拒绝时，交互流程引导打开系统权限设置，由用户手动授权后重测；不会自行授权，也不建议给 launchd/xpcproxy 全盘权限。给 Python 完全磁盘访问权限会扩大使用该解释器的其他脚本的访问范围，可取消并选择迁移到非受保护目录。非交互安装检测失败则直接退出，不更换原任务。`status` 反映注册状态，领取结果以 `checkin.log` 为准；预检通过不代表网络接口或微信推送必定成功。系统升级或移动路径后可重复安装刷新任务。

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
执行 `python3 checkin_cli.py wizard`，设置时间后选择「2 浏览器扫码绑定微信」，即可在主向导内完成通知配置。也可执行 `python3 checkin_cli.py wx-bind` 单独绑定。扫码登录后自动读取凭证、复用或创建模板、识别接收者，无需手填 AppID/AppSecret。

签到文案参照 workbuddy-checkin：每个账号单独通知，标题带显示名称，例如 `[我的主账号] 🎉 Qoder 自动签到成功`。名称优先级为自定义 `display_name` → 登录凭证中的 `nickname` → 内部账号标识 `name`。官方 CLI 登录导入时保存昵称；旧凭证需重新导入才能获得昵称。手动 Token 没有昵称时回退内部标识。向导添加账号时可输入中文名称或留空使用昵称；已有账号在账号管理输入 `n` 修改显示名称，留空恢复昵称，不改变账号绑定或凭证路径。说明包含地区、执行结果及有效的本轮 Credits；已领取不会描述为本次新增奖励，失败不展示奖励或原始异常。沿用现有三个 keyword 模板，无需重新绑定。

多账号通过 `wizard` 的账号管理步骤添加：输入 `a` → 唯一账号名 → 地区 → 浏览器登录或手动 Token；可连续添加同地区账号。新增账号自动分配独立 `token_file` 和 `token_env`，浏览器登录自动绑定 `expected_account_id`。新增账号使用临时独立 CLI 登录缓存，不复用此前 CLI 登录态；系统浏览器仍可能自动选中已登录账号，需要手动切换。若同地区身份已绑定会拒绝重复添加；手动 Token 未绑定身份时无法提前识别重复身份。国内站浏览器授权仍未适配，请选择手动 Token。

已有账号不会被覆盖，新增账号后保存的调度地区为 `both`，重新 `install` 后运行全部配置账号。所有账号共用顶层微信接收配置，每次执行按账号分别通知；一条推送失败仍继续尝试其他账号，下一次执行再次通知全部账号。`--dry-run` 不发送通知。向导中途取消不会保存账号列表，但此前已生成的独立凭证文件可能保留，不会覆盖原凭证。

模板内容与参考项目一致：
```
结果：{{keyword1.DATA}}
说明：{{keyword2.DATA}}
时间：{{keyword3.DATA}}
```

执行 `python3 checkin_cli.py test-notify` 发送测试通知；接口受理不代表手机一定收到，请在微信确认。

也可以使用浏览器授权自动绑定测试号，避免手填 AppID、AppSecret、OpenID：

```bash
python3 checkin_cli.py wx-bind
```

缺少 Playwright 时自动准备项目内独立环境，不向 Homebrew Python 安装依赖。向导选择「2 浏览器扫码绑定微信」与 `wx-bind` 使用同一流程。

命令打开微信公众平台测试号页面，由本人扫码登录；程序读取 AppID/AppSecret，通过官方 API 查找兼容模板。已有模板直接复用（优先当前模板）；没有则在网页新增「Qoder 签到通知」，再通过 API 确认，不删除或修改其他模板。已绑定或唯一关注者自动选用；没有关注者时等待扫码关注，多人时可选择序号。全部步骤完成才保存配置；失败、取消均保留原配置。若模板已在服务端创建但后续绑定失败，下次会复用该模板。
配置字段沿用参考项目：`notify_channel: "wx_test"`、`wx_test_appid`、`wx_test_secret`、`wx_test_touser`、`wx_test_template_id`。设置 `notify_channel: "none"` 可关闭。配置文件权限为 0600；不提交密钥到 Git。

本机定时任务使用 `checkin_cli.py run`，默认每次执行都发送结果，包括“本轮已签到”，无需重装定时任务。手动 `run`、安装和登录补跑也会发送；`--dry-run` 不推送；直接运行底层 `qoder_checkin.py` 不推送。通知包含账号名称、地区、结果及积分，不包含 Qoder Token、设备信息或原始异常。默认忽略旧 `.notify-state.json`，不删除它。只有显式设置 `notify_dedupe: true` 才启用旧的同周期同结果去重规则。

网络请求超时或微信拒绝会记录通知失败并返回非零退出码，不重做领取，也不自动重发不确定的消息；默认模式仍尝试其他账号，下次计划运行再次发送全部账号结果。接口受理不等于手机已送达，无法保证严格一次送达。当前接入微信测试号，不含 pushplus 或通用 webhook。


`wx-setup` 和向导的「4 手动配置」保留手动 AppID/AppSecret 方式作为备用。向导保存后可选择发送测试通知；`wx-bind` 本身不发送消息，可运行 `test-notify` 验证推送。
