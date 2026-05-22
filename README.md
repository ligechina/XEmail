# XEmail

Self-hosted email assistant. Talks SMTP/IMAP directly to your mailbox and auto-classifies messages with a two-stage engine: natural-language rules compiled to a deterministic local program, with optional LLM fallback for the rest. Multi-user, multi-account, bilingual zh/en UI; credentials encrypted at rest with Fernet.

XEmail 是一个本地运行的智能邮件助手：一个 FastAPI 后端 + 纯前端 Web 界面，
通过 SMTP/IMAP 直接和你的邮箱服务器通信，并可选地用大语言模型（LLM）
按你写好的规则与 prompt 把邮件自动归档到自定义文件夹。

> ⚠️ XEmail 把账号配置（含邮箱授权码）和邮件正文都存在本机的 `data/` 目录下。
> 仅推荐在个人电脑或受信任的内网环境运行，不要把 `data/` 暴露到公网或提交到 Git。

## 主要特性

- **多账号管理**：每个用户可以挂载多个邮箱账号，支持 SMTP/IMAP（SSL/STARTTLS）。
- **本地分类引擎**：
  - **固定规则（rule_program）**：用自然语言描述规则，引擎本地编译执行，
    命中后直接落入指定文件夹，**不会调用 LLM**。
  - **LLM 兜底**：未命中规则的邮件再交给 LLM 按你定义的 prompt 与文件夹列表分类。
  - 始终保留两个系统文件夹：`垃圾邮件`、`未分类`。
- **多用户 + 权限边界**：内置用户体系（admin / normal），账号归属严格隔离；
  普通用户只看到自己的账号，admin 可以在 `/admin` 跨用户管理。
- **可选 LLM 接入**：API Key 加密存放在 `data/config.json`（密钥来自 `data/.llm_secret`）。
- **附件、联系人、草稿、已发送**：本地持久化为独立的 JSON 文件。
- **i18n**：内置中 / 英双语界面。

## 目录结构

```
app/                FastAPI 后端
  main.py           路由与 API
  models.py         Pydantic 数据模型
  storage.py        JSON 文件存储层（带原子写、备份轮转、迁移）
  services/
    auth.py         用户认证、密码哈希、会话
    email_client.py SMTP / IMAP 客户端封装
    rule_program.py 自然语言规则 → 本地可执行程序
    spam_filter.py  LLM 分类与默认 prompt
web/                纯前端（HTML + Vanilla JS）
scripts/            macOS 启停脚本与发布打包脚本
docs/               用户手册与设计文档
data/               运行时数据（默认被 .gitignore 忽略）
  config.example.json  ← 配置文件示例，复制为 config.json 后再填入你的账号
```

## 快速启动

需要 Python 3.10+ 。

```bash
git clone https://github.com/<your-user>/XEmail.git
cd XEmail

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload --port 8000
```

打开浏览器访问：<http://127.0.0.1:8000>

首次启动会自动在 `data/` 下创建空的 JSON 文件，并要求你创建第一个用户（默认就是
管理员）。登录后进入「设置」配置 SMTP / IMAP 与（可选的）LLM Key 即可使用。

> macOS 用户也可以直接双击 `scripts/start.command` 后台启动，再用
> `scripts/stop.command` 关闭。

## 配置示例

`data/config.example.json` 给出了一个最小可用的账号配置形状，
**不包含任何真实凭据**。建议的做法是：

1. 不要直接修改 `config.example.json`；
2. 通过 Web UI 的「设置」页录入账号，让程序自己生成 `data/config.json`；
3. 或者复制示例文件为 `config.json` 后手工填入，重启服务生效。

## 数据与隐私

XEmail 是单机部署应用，所有数据都保存在本机：

| 文件 | 内容 | 备注 |
| --- | --- | --- |
| `data/config.json` | 账号、SMTP/IMAP（密码加密）、LLM Key（加密） | **绝不提交到 Git**；明文密码不落盘 |
| `data/users.json` | 用户名 + 密码哈希（pbkdf2_sha256） | |
| `data/emails.json` | 已收取邮件的元数据与正文 | |
| `data/sent.json` | 已发送邮件记录 | |
| `data/drafts.json` | 草稿 | |
| `data/folders.json` | 每个账号的自定义文件夹结构 | |
| `data/contacts.json` | 联系人 | |
| `data/prompts.json` | 用户自定义 LLM 系统 prompt 与规则 | |
| `data/attachments/<record_id>/` | 附件原文件 | |
| `data/.session_secret` | Web 会话签名密钥 | 首次启动随机生成 |
| `data/.llm_secret` | LLM API Key 加密密钥 | 首次启动随机生成 |

仓库内的 `.gitignore` 已经把 `data/*` 整个目录（除 `.gitkeep` 与
`config.example.json` 外）排除在 Git 之外。请不要修改这条规则。

## 安全说明

- 邮箱授权码与 LLM API Key 都使用 `cryptography` 的 Fernet（AES-128-CBC + HMAC-SHA256）
  对称加密后才写入 `data/config.json`，对应字段分别为 `sender_password_enc` 与
  `llm.api_key_enc`，**明文永远不会落盘**。
- 加密密钥保存在 `data/.llm_secret`（权限 0o600），与 `config.json` 分离。
  泄露 `config.json`（备份、误传、截屏）本身不会暴露密码或 API Key。
- 从旧版本升级时，如果 `config.json` 中检测到明文 `sender_password`，
  程序在首次读取配置时会自动加密并改写文件，同时删除所有
  `config.json.bak.*` 备份以清除残留明文。
- 默认监听 `127.0.0.1`，不要把它暴露到 `0.0.0.0` 或公网；如确需远程访问，
  请前置反向代理 + HTTPS + 基础鉴权。

## API 概览

| Method | Path | 说明 |
| --- | --- | --- |
| `GET`  | `/api/config`   | 获取当前用户可见的账号与系统配置 |
| `POST` | `/api/config`   | 保存账号配置 |
| `POST` | `/api/send`     | 通过激活账号发送邮件 |
| `POST` | `/api/receive`  | 拉取邮件并按规则 + LLM 分类；可选 `?limit=N` |
| `GET`  | `/api/emails`   | 获取邮件列表，支持 `?category=...` 过滤 |
| `POST` | `/api/login`    | 用户登录 |
| `POST` | `/api/users`    | 注册（首次访问自动开放） |

完整路由以 `app/main.py` 为准。

## 致谢

XEmail 起步于一个个人项目，目标是把"自动分类"这件事做得既本地、又可解释。
欢迎 issue / PR。

## License

本项目基于 [木兰宽松许可证, 第2版（Mulan PSL v2）](./LICENSE) 开源发布。

Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.

详细条款见仓库根目录下的 `LICENSE` 文件，或访问
<http://license.coscl.org.cn/MulanPSL2> 获取最新版本。
