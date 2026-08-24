# Tibo Codex 额度重置提醒

追踪 Tibo（Thibault Sottiaux，`@thsottiaux`）在 X 上发布的 Codex 额度重置相关消息，把发帖时间换算成北京时间，并通过 SMTP 发到指定邮箱。

程序只依赖 Python 标准库，支持 Windows、macOS 和 Linux。它保留 Tibo 的原创帖与回复，排除转帖；只有正文同时命中“Codex/ChatGPT Work”和“reset/usage limit/quota”等两组语义词时才通知。

## 1. 准备凭证

你需要：

- X Developer 账号、Project/App 以及 App 的 Bearer Token；
- 一个支持 SMTP 的发件邮箱和 SMTP 授权码；
- 收件邮箱地址（可以与发件邮箱相同）。

X API v2 目前按使用量计费。在 X Developer Console 中购买或配置额度并设置预算上限。此程序使用[公开用户帖子时间线接口](https://docs.x.com/x-api/users/get-posts)，不需要登录 Tibo 的账号。计费方式与月度上限请看 [X API Usage and Billing](https://docs.x.com/x-api/fundamentals/post-cap)。

## 2. 配置

安装 Python 3.10 或更高版本，然后在项目目录执行：

```powershell
Copy-Item .env.example .env
notepad .env
```

至少填写：

```dotenv
X_BEARER_TOKEN=你的_X_Bearer_Token
SMTP_HOST=邮箱服务商的_SMTP_主机
SMTP_PORT=465
SMTP_USERNAME=发件邮箱地址
SMTP_PASSWORD=SMTP授权码_通常不是网页登录密码
MAIL_FROM=发件邮箱地址
MAIL_TO=你的收件邮箱地址
SMTP_USE_SSL=true
SMTP_STARTTLS=false
```

多个收件人用英文逗号分隔。若服务商要求端口 587，将 `SMTP_USE_SSL=false`、`SMTP_STARTTLS=true`。具体主机、端口和授权码开通方式以邮箱服务商说明为准。

## 3. 验证

先测试邮件配置：

```powershell
python .\watcher.py --test-email
```

再读取 X 并预览匹配结果；此命令不发邮件，也不写状态：

```powershell
python .\watcher.py --once --dry-run --verbose
```

正式运行一次：

```powershell
python .\watcher.py --once
```

首次运行默认检查过去 24 小时，可能会补发这段时间内的匹配消息。可通过 `FIRST_RUN_LOOKBACK_HOURS` 修改。之后程序使用 `.state/tibo-reset-watcher.json` 中的帖子 ID 去重。

## 4. 持续运行

### 方式 A：常驻进程

```powershell
python .\watcher.py --daemon
```

默认每 300 秒检查一次。关闭终端会停止程序。

### 方式 B：Windows 任务计划程序（推荐）

打开“任务计划程序”并创建基本任务：

1. 触发器选择“每天”；创建后在属性中将“重复任务间隔”设为 5 或 15 分钟，持续时间设为“无限期”。
2. 操作选择“启动程序”。
3. 程序填写 `powershell.exe`。
4. 参数填写 `-NoProfile -ExecutionPolicy Bypass -File "项目绝对路径\run_once.ps1"`。
5. 勾选“如果任务运行时间超过下一次计划时间，则不启动新实例”。

每次任务只运行一轮；状态文件负责去重。调大间隔可以降低 X API 调用量。

## 匹配与可靠性

- 使用 X API 的 `created_at`（UTC）转换为固定 UTC+8 的北京时间，不依赖电脑当前时区。
- 支持 X 长帖的完整 `note_tweet` 正文。
- 邮件发送成功后才推进状态；发送失败时，下次会重试同一条匹配帖子。
- 非匹配帖子也会推进游标，避免反复读取。
- 关键词匹配可能存在误报或漏报。可以在 `.env` 中覆盖 `PRODUCT_KEYWORDS` 与 `RESET_KEYWORDS`。
- 通知只代表 Tibo 发布了相关内容，实际额度是否重置、适用套餐和生效时间应以原帖及你的 Codex 账户页面为准。

## 常见问题

`X API HTTP 401/403`：检查 Bearer Token、App 权限与 X API 余额。  
`X API HTTP 429`：请求过于频繁，增大 `POLL_INTERVAL_SECONDS`。  
SMTP 登录失败：确认已开启 SMTP，使用授权码，并核对 SSL/STARTTLS 与端口组合。  
想重新补查：备份后删除 `.state/tibo-reset-watcher.json`，程序会按首次运行回溯窗口重新处理。
