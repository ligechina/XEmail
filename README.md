# XEmail

> Self-hosted email assistant. Talks SMTP/IMAP directly to your mailbox and
> auto-classifies incoming mail with a two-stage engine: natural-language rules
> compiled to a deterministic local program, with optional LLM fallback for
> the rest. Multi-user, multi-account, bilingual zh/en UI; credentials
> encrypted at rest with Fernet.
>
> 本地运行的智能邮件助手：一个 FastAPI 后端 + 纯前端 Web 界面。直接通过
> SMTP/IMAP 与邮箱服务器通信，并可选地用大语言模型按你写好的规则与 prompt
> 把邮件自动归档到自定义文件夹。多用户、多账号、中英双语界面；账号凭据使用
> Fernet 加密落盘。

> ⚠️ XEmail stores account configuration (mailbox app passwords) and message
> bodies under the local `data/` directory. Only run it on your personal
> computer or a trusted intranet — never expose `data/` publicly or commit it
> to Git.
>
> ⚠️ XEmail 把账号配置（含邮箱授权码）和邮件正文都存在本机的 `data/` 目录下。
> 仅推荐在个人电脑或受信任的内网环境运行，不要把 `data/` 暴露到公网或提交到 Git。

---

## Install / 安装

The easiest way is the prebuilt installer for your OS.
最简单的方式是使用对应平台的预编译安装包。

| Platform | Artifact | Notes |
| --- | --- | --- |
| macOS (Apple Silicon) | `xemail-macos-installer-*.pkg` | Double-click to install into `/Applications/XEmail.app`. Bundles a self-contained Python runtime. |
| Windows | `xemail-windows-installer-*.zip` (未经测试) | Unzip, then run `install_windows.bat`. Requires Python 3.10+ on PATH (`py -3`). |
| Server / source | `xemail-release-*.tar.gz` or this repo | See [Run from source](#run-from-source--从源码运行) below. |

Release binaries are published on the
[GitHub Releases page](https://github.com/ligechina/XEmail/releases).
发布版二进制可在 GitHub Releases 页下载。

---

## Highlights / 主要特性

- **Multi-account mailboxes** — each user can attach several mailboxes; SMTP/IMAP over SSL or STARTTLS.
  **多账号管理**：每个用户可挂载多个邮箱账号，支持 SMTP/IMAP（SSL / STARTTLS）。
- **Two-stage local classifier:**
  **本地两阶段分类引擎：**
  - **Rule program** — describe rules in plain language; the engine compiles them to a local program. Matching messages go straight to the target folder and **never hit the LLM**.
    **固定规则（rule_program）**：用自然语言写规则，本地编译为可执行程序；命中即归档，**不会调用 LLM**。
  - **LLM fallback** — anything the rule program doesn't cover is handed to the LLM with your custom prompt and folder list.
    **LLM 兜底**：未命中规则的邮件交给 LLM 按你定义的 prompt 与文件夹列表归类。
  - Two folders are always reserved: `垃圾邮件` (spam) and `未分类` (unclassified).
    始终保留两个系统文件夹：`垃圾邮件`、`未分类`。
- **Users & isolation** — built-in user system (admin / normal); accounts are strictly scoped to their owner; admins get cross-user management at `/admin`.
  **多用户 + 权限边界**：内置 admin / normal 用户体系；账号归属严格隔离；admin 在 `/admin` 跨用户管理。
- **Optional LLM** — API key encrypted at rest in `data/config.json` (key material in `data/.llm_secret`).
  **可选 LLM 接入**：API Key 加密存放在 `data/config.json`（密钥来自 `data/.llm_secret`）。
- **Attachments, contacts, drafts, sent** — each persisted as its own JSON file.
  **附件、联系人、草稿、已发送**：本地持久化为独立的 JSON 文件。
- **Desktop mode** — `pywebview` launcher wraps the web UI in a native window, with tray support on macOS.
  **桌面模式**：`pywebview` 把 Web UI 包成原生窗口，macOS 上支持托盘。
- **i18n** — built-in Chinese / English interface.
  **i18n**：内置中 / 英双语界面。

---

## Project layout / 目录结构

```
app/                FastAPI backend / FastAPI 后端
  main.py           routes & API / 路由与 API
  models.py         Pydantic data models / 数据模型
  storage.py        JSON storage layer (atomic write, rotating backup, migration)
                    JSON 文件存储层（原子写、备份轮转、迁移）
  services/
    auth.py         users, password hashing, sessions / 认证与会话
    email_client.py SMTP / IMAP wrapper / SMTP / IMAP 客户端
    rule_program.py natural-language rules → local program / 规则编译执行
    spam_filter.py  LLM classifier + default prompt / LLM 分类与默认 prompt
web/                static frontend (HTML + vanilla JS) / 纯前端
desktop/            pywebview launcher + tray / 桌面启动器与托盘
scripts/            macOS start/stop helpers, release & installer builders
                    macOS 启停脚本、发布包与安装包构建脚本
docs/               user guide and design notes / 用户手册与设计文档
data/               runtime data (gitignored) / 运行时数据（默认被忽略）
  config.example.json   shape of the account config / 配置文件示例
```

---

## Run from source / 从源码运行

Requires Python 3.10+ .
需要 Python 3.10 及以上。

```bash
git clone https://github.com/ligechina/XEmail.git
cd XEmail

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload --port 8000
```

Open <http://127.0.0.1:8000> in your browser.
浏览器打开 <http://127.0.0.1:8000>。

On first launch, XEmail creates empty JSON files under `data/` and asks you to
register the first user (who becomes the administrator). After login, head to
"Settings" to configure SMTP / IMAP and an optional LLM key.
首次启动会在 `data/` 下创建空 JSON，并要求你创建第一个用户（即管理员）。
登录后进入「设置」配置 SMTP / IMAP 与（可选的）LLM Key。

> macOS users can also double-click `scripts/start.command` to run the server
> in the background, and `scripts/stop.command` to stop it.
>
> macOS 用户也可以直接双击 `scripts/start.command` 后台启动、用
> `scripts/stop.command` 关闭。

---

## Desktop mode / 桌面模式

If you prefer a standalone window over a browser tab:
如果你希望以独立桌面窗口使用 XEmail：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m desktop
```

Or on macOS, double-click:
或在 macOS 上直接双击：

```
scripts/start_desktop.command
```

Notes / 说明：

- The desktop window auto-launches the local FastAPI backend on `127.0.0.1:8000`. If a usable XEmail backend is already on that port, it is reused.
  桌面窗口会自动拉起本地 FastAPI 后端（默认 `127.0.0.1:8000`）；若该端口上已有可用后端，则直接复用。
- Desktop-mode data directory defaults to:
  桌面模式数据目录默认位于：
  - macOS: `~/Library/Application Support/XEmail/data`
  - other: `~/.xemail/data`
- On first launch the desktop app shows a folder picker for the data directory. New versions ask once again (pre-filled with the previous choice).
  首次启动会弹窗询问数据目录；安装新版本后首次启动会再次确认一次（默认带入上次选择）。
- First desktop launch will migrate historical data from the project's `data/` directory if present.
  首次桌面启动会尝试从项目根目录的 `data/` 自动迁移历史数据。
- Backend logs live in the app runtime dir (e.g. `~/Library/Application Support/XEmail/runtime/desktop-backend.log` on macOS).
  后端日志写入应用运行目录（如 macOS 下的 `~/Library/Application Support/XEmail/runtime/desktop-backend.log`）。
- Environment overrides: `XEMAIL_APP_DIR` (app root), `XEMAIL_DATA_DIR` (backend data dir).
  可通过 `XEMAIL_APP_DIR` 覆盖应用根目录，或通过 `XEMAIL_DATA_DIR` 直接指定后端数据目录。

### Auto-start on login (macOS) / 开机启动（macOS）

Enable / disable / check via CLI:
通过命令启用 / 关闭 / 查看状态：

```bash
python -m desktop --enable-autostart
python -m desktop --disable-autostart
python -m desktop --autostart-status
```

Or double-click the corresponding `.command` in `scripts/`:
或直接双击 `scripts/` 下对应脚本：

- `scripts/enable_autostart.command`
- `scripts/disable_autostart.command`
- `scripts/autostart_status.command`

Implemented as a user-level LaunchAgent at
`~/Library/LaunchAgents/org.xemail.desktop.plist`.
实现方式为用户级 LaunchAgent（`~/Library/LaunchAgents/org.xemail.desktop.plist`）。

### Tray mode (optional) / 托盘模式（可选）

Enable "close window → minimize to tray":
启用「关闭窗口后最小化到托盘」：

```bash
python -m desktop --enable-tray
```

Or double-click `scripts/start_desktop_tray.command`.
或双击 `scripts/start_desktop_tray.command`。

Tray dependencies (install if needed):
托盘模式依赖（按需安装）：

```bash
pip install pystray pillow
```

---

## Build installers / 自行打包

The release artifacts (macOS `.pkg`, Windows `.zip`, generic source tarball)
are produced by scripts under `scripts/`:
发布版的安装包由 `scripts/` 下的脚本生成：

```bash
# macOS .pkg + Windows .zip (writes to dist/)
# 生成 macOS .pkg 与 Windows .zip（输出到 dist/）
bash scripts/package_installers.sh

# Generic source tarball for server / manual deployment
# 通用源码发布包（服务端 / 手工部署用）
bash scripts/package_release.sh
```

`package_installers.sh` builds a self-contained Python runtime inside the
macOS bundle (`Contents/Resources/runtime/`) and writes a launcher script at
`Contents/MacOS/XEmail` that:
`package_installers.sh` 会在 macOS bundle 内部 (`Contents/Resources/runtime/`)
构建自包含的 Python 运行时，并在 `Contents/MacOS/XEmail` 写入启动器脚本，它会：

1. Force a `lsregister -f` so LaunchServices has the bundle registered before exec.
   强制 `lsregister -f`，确保 LaunchServices 在 exec 之前注册到 bundle。
2. Export `__CFBundleIdentifier=com.xemail.app` so the post-exec Python process keeps the XEmail bundle identity.
   设置 `__CFBundleIdentifier=com.xemail.app`，保证 exec 之后的 Python 进程继续认领 XEmail bundle。
3. `exec` into a hard-link of the bundled Python whose filename matches `CFBundleExecutable` — this is what gives the Dock icon the correct XEmail.icns.
   `exec` 到一个文件名与 `CFBundleExecutable` 匹配的 Python 硬链接 —— Dock 图标显示正确 XEmail.icns 的关键。

### macOS diagnostics / macOS 诊断脚本

If a release behaves oddly (wrong Dock icon, can't quit from the Dock, ghost
icons after install) the following scripts under `scripts/` help isolate it:
如果某个发布版表现异常（Dock 图标不对、Dock 上右键退不掉、装包后 Dock 有
幽灵图标），下面这几个 `scripts/` 中的脚本可以辅助定位：

- `diag_dock.sh` — process tree + LaunchServices state for the running XEmail.
- `diag_bundle.sh` — env vars and `NSBundle.mainBundle()` view from inside the running Python.
- `diag_dock_state.sh` — Dock persistent / recent entries and helper PID types.
- `reset_before_install.sh` — kill all XEmail processes, uninstall the .app, reset LaunchServices, restart Dock. Run before installing a fresh `.pkg` to get a clean state.

---

## Configuration example / 配置示例

`data/config.example.json` shows the minimum shape of a workable account
configuration. It contains **no real credentials**. Recommended workflow:
`data/config.example.json` 给出最小可用配置形状，**不含任何真实凭据**。
建议做法：

1. Do not edit `config.example.json` directly.
   不要直接修改 `config.example.json`。
2. Use the web UI's Settings page to enter your account — the server generates `data/config.json` for you.
   通过 Web UI 的「设置」页录入账号，让程序自己生成 `data/config.json`。
3. Or copy the example to `config.json`, fill it in by hand, and restart.
   或者复制示例为 `config.json` 后手工填入，重启生效。

---

## Data & privacy / 数据与隐私

XEmail is a single-machine deployment; all data stays local.
XEmail 是单机部署应用，所有数据都保存在本机。

| File / 文件 | Content / 内容 | Notes / 备注 |
| --- | --- | --- |
| `data/config.json` | accounts, SMTP/IMAP (password encrypted), LLM key (encrypted) / 账号、SMTP/IMAP（密码加密）、LLM Key（加密） | **Never commit** / **绝不提交到 Git**；明文密码不落盘 |
| `data/users.json` | username + password hash (pbkdf2_sha256) / 用户名 + 密码哈希 | |
| `data/emails.json` | fetched-mail metadata + bodies / 邮件元数据与正文 | |
| `data/sent.json` | sent-mail records / 已发送邮件 | |
| `data/drafts.json` | drafts / 草稿 | |
| `data/folders.json` | per-account custom folder structure / 每账号自定义文件夹结构 | |
| `data/contacts.json` | contacts / 联系人 | |
| `data/prompts.json` | per-user LLM system prompts & rules / 用户 LLM prompt 与规则 | |
| `data/attachments/<record_id>/` | attachment files / 附件原文件 | |
| `data/.session_secret` | web session signing key / Web 会话签名密钥 | auto-generated on first run / 首次启动生成 |
| `data/.llm_secret` | LLM API key encryption key / LLM Key 加密密钥 | auto-generated on first run / 首次启动生成 |

The repo's `.gitignore` already excludes the entire `data/` directory except
`config.example.json` and the `.gitkeep` placeholders. Do not relax that rule.
仓库 `.gitignore` 已经把 `data/*` 整个目录（除 `.gitkeep` 与
`config.example.json` 外）排除在 Git 之外。请不要修改这条规则。

---

## Security notes / 安全说明

- Mailbox app passwords and the LLM API key are encrypted with `cryptography`'s
  Fernet (AES-128-CBC + HMAC-SHA256) before being written to `data/config.json`,
  as `sender_password_enc` and `llm.api_key_enc`. **Cleartext never lands on disk.**
  邮箱授权码与 LLM API Key 都使用 `cryptography` 的 Fernet (AES-128-CBC + HMAC-SHA256)
  对称加密后才写入 `data/config.json`，对应字段分别为 `sender_password_enc` 与
  `llm.api_key_enc`，**明文永远不会落盘**。
- The encryption key lives in `data/.llm_secret` (perms `0o600`), separate
  from `config.json`. Leaking `config.json` alone (backups, screenshots, mis-sends)
  does not expose your passwords or the API key.
  加密密钥保存在 `data/.llm_secret`（权限 0o600），与 `config.json` 分离。
  仅泄露 `config.json` 本身不会暴露密码或 API Key。
- Upgrading from older builds: if `config.json` still contains a plaintext
  `sender_password`, it is encrypted in place on first read and any
  `config.json.bak.*` backups are purged to clear residual cleartext.
  从旧版升级时，如果 `config.json` 中检测到明文 `sender_password`，
  程序在首次读取配置时会自动加密并改写文件，同时删除所有
  `config.json.bak.*` 备份以清除残留明文。
- The server binds `127.0.0.1` by default. Do **not** expose it to `0.0.0.0`
  or the public internet directly; if you need remote access, front it with
  a reverse proxy + HTTPS + auth.
  默认监听 `127.0.0.1`，不要把它暴露到 `0.0.0.0` 或公网；如确需远程访问，
  请前置反向代理 + HTTPS + 基础鉴权。

---

## API overview / API 概览

| Method | Path | Description / 说明 |
| --- | --- | --- |
| `GET`  | `/api/config`   | List accounts and system config visible to the current user / 获取当前用户可见的账号与系统配置 |
| `POST` | `/api/config`   | Save account config / 保存账号配置 |
| `POST` | `/api/send`     | Send mail via the active account / 通过激活账号发送邮件 |
| `POST` | `/api/receive`  | Fetch mail and classify (rule + LLM); supports `?limit=N` / 拉取邮件并按规则 + LLM 分类 |
| `GET`  | `/api/emails`   | List mail; supports `?category=...` / 获取邮件列表，支持 `?category=...` 过滤 |
| `POST` | `/api/login`    | User login / 用户登录 |
| `POST` | `/api/users`    | Register (open on first visit) / 注册（首次访问自动开放） |

The full route table lives in `app/main.py`.
完整路由以 `app/main.py` 为准。

---

## Contributing / 参与

Bugs, feature ideas and PRs all welcome at
<https://github.com/ligechina/XEmail/issues>.
欢迎在 <https://github.com/ligechina/XEmail/issues> 提 issue 或 PR。

---

## License / 许可

This project is released under the
[Mulan Permissive Software License v2 (Mulan PSL v2)](./LICENSE).
本项目基于[木兰宽松许可证, 第2版（Mulan PSL v2）](./LICENSE)开源发布。

Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.

See `LICENSE` in the repo root, or
<http://license.coscl.org.cn/MulanPSL2> for the latest version of the license text.
详细条款见仓库根目录下的 `LICENSE` 文件，或访问
<http://license.coscl.org.cn/MulanPSL2> 获取最新版本。
