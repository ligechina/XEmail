/*
 * Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
 * XEmail is licensed under Mulan PSL v2.
 * You can use this software according to the terms and conditions of the Mulan PSL v2.
 * You may obtain a copy of Mulan PSL v2 at:
 *          http://license.coscl.org.cn/MulanPSL2
 * THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
 * EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
 * MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
 * See the Mulan PSL v2 for more details.
 */

/* XEmail i18n module.
 *
 * Strategy: Chinese is the canonical "key". The dictionary maps each Chinese
 * string to its translation(s). On apply we walk the DOM, replace matching
 * text nodes / attributes, and cache the original so we can swap back.
 *
 * For dynamic strings in JS code, call `t("中文")` (or `t.fmt("中文，${x}", {x})`)
 * to get the current language's string. Pages that build HTML dynamically
 * should call `I18n.apply()` after insertion so newly added nodes get
 * translated. New strings show up untranslated until added to the dictionary;
 * that's fine — the Chinese source still renders.
 */
(function (global) {
  "use strict";

  var STORAGE_KEY = "xemail_lang";

  // Dictionary: { "<zh>": { en: "<English>" } }
  // Keys are stored verbatim (trimmed). Whitespace surrounding a text node
  // is preserved by the walker — don't include leading/trailing spaces here.
  var DICT = {
    // ============ common UI ============
    "登录": { en: "Sign In" },
    "注册": { en: "Register" },
    "登录 · XEmail": { en: "Sign In · XEmail" },
    "设置管理员": { en: "Create Administrator" },
    "系统尚未初始化，请创建第一位管理员账号。该账号将拥有全部权限。": {
      en: "The system has not been initialized. Create the first administrator account; this account will have all privileges.",
    },
    "创建管理员": { en: "Create Administrator" },
    "用户名": { en: "Username" },
    "密码": { en: "Password" },
    "密码（至少 6 位）": { en: "Password (at least 6 characters)" },
    "用户名（3–32 位）": { en: "Username (3–32 characters)" },
    "注册并登录": { en: "Register & Sign In" },
    "无法连接服务端": { en: "Cannot reach the server" },
    "请填写用户名和密码": { en: "Please enter username and password" },
    "处理中…": { en: "Processing…" },
    "管理员已创建，正在进入…": { en: "Administrator created. Redirecting…" },
    "登录成功，正在进入…": { en: "Signed in. Redirecting…" },
    "注册成功，正在进入…": { en: "Registered. Redirecting…" },
    "请求失败": { en: "Request failed" },
    "保存": { en: "Save" },
    "保存中…": { en: "Saving…" },
    "已保存": { en: "Saved" },
    "保存失败": { en: "Failed to save" },
    "取消": { en: "Cancel" },
    "关闭": { en: "Close" },
    "确认": { en: "Confirm" },
    "确定": { en: "OK" },
    "删除": { en: "Delete" },
    "编辑": { en: "Edit" },
    "全部": { en: "All" },
    "重要": { en: "Important" },
    "其他": { en: "Other" },
    "未分类": { en: "Uncategorized" },
    "已发送": { en: "Sent" },
    "已处理": { en: "Done" },
    "已回复": { en: "Replied" },
    "回复": { en: "Reply" },
    "转发": { en: "Forward" },
    "再次编辑": { en: "Edit Again" },
    "曾经重要": { en: "Previously Important" },
    "刷新": { en: "Refresh" },
    "退出": { en: "Sign Out" },
    "就绪": { en: "Ready" },
    "加载中": { en: "Loading" },
    "加载中…": { en: "Loading…" },
    "提示": { en: "Prompts" },
    "说明": { en: "Details" },
    "更多": { en: "More" },
    "置顶": { en: "Pin" },
    "标签": { en: "Tags" },
    "无主题": { en: "(No subject)" },
    "(无主题)": { en: "(No subject)" },
    "(无正文)": { en: "(No body)" },
    "无正文": { en: "(No body)" },
    "(未命名邮件)": { en: "(Unnamed email)" },
    "未命名": { en: "Unnamed" },
    "未命名账号": { en: "Unnamed account" },
    "(当前)": { en: "(current)" },
    "通过": { en: "passed" },
    "失败": { en: "failed" },
    "请填写理由后再确认": { en: "Please enter a reason before confirming" },
    "处理失败": { en: "Processing failed" },
    "服务器内部错误": { en: "Internal server error" },
    "未登录，正在跳转…": { en: "Not signed in. Redirecting…" },
    "✕ 关闭": { en: "✕ Close" },
    "关闭本页": { en: "Close this tab" },
    "激活": { en: "Activate" },
    "已激活": { en: "Active" },
    "中文": { en: "Chinese" },
    "英文": { en: "English" },
    "生成语言": { en: "Generation language" },

    // ============ settings page ============
    "邮箱设置 · XEmail": { en: "Email Settings · XEmail" },
    "邮箱设置": { en: "Email Settings" },
    "管理多个邮箱账号，可随时切换 / 编辑 / 删除。": {
      en: "Manage multiple email accounts — switch, edit, or delete at any time.",
    },
    "大模型（DeepSeek V4）": { en: "Language Model (DeepSeek V4)" },
    "用于智能分类的 DeepSeek API Key。必填，否则主页将弹窗提醒。仅管理员可修改； 密钥保存在服务端，不会回显给前端。": {
      en: "DeepSeek API Key used for AI classification. Required; otherwise the inbox will warn you. Admin-only; the key is stored server-side and never echoed back.",
    },
    "DeepSeek API Key": { en: "DeepSeek API Key" },
    "保存 API Key": { en: "Save API Key" },
    "邮箱账号": { en: "Email Accounts" },
    "支持多账号。点击「激活」即可在主页切换收件箱；点击「编辑」可修改账号详情。": {
      en: "Multiple accounts supported. Click \"Activate\" to switch the inbox; click \"Edit\" to modify account details.",
    },
    "+ 新增账号": { en: "+ Add Account" },
    "← 返回账号列表": { en: "← Back to Account List" },
    "← 返回用户管理": { en: "← Back to User Management" },
    "正在编辑账号": { en: "Editing account" },
    "邮箱信息": { en: "Account Info" },
    "账号显示名称（仅用于在主页和设置里区分）。": {
      en: "Display name (used only to distinguish accounts on the inbox and settings pages).",
    },
    "账号名称（仅用于标识）": { en: "Account name (label only)" },
    "登录凭据": { en: "Login Credentials" },
    "用于收发邮件的邮箱地址与授权码。": {
      en: "Email address and authorization code used to send and receive mail.",
    },
    "发件邮箱": { en: "Sender email" },
    "邮箱密码 / 授权码": { en: "Email password / auth code" },
    "默认收件邮箱": { en: "Default receiver email" },
    "SMTP（发件）": { en: "SMTP (Outgoing)" },
    "用于发送邮件的服务器。": { en: "Server used for outgoing mail." },
    "SMTP 主机": { en: "SMTP host" },
    "SMTP 端口": { en: "SMTP port" },
    "使用 SSL": { en: "Use SSL" },
    "使用 STARTTLS": { en: "Use STARTTLS" },
    "IMAP（收件）": { en: "IMAP (Incoming)" },
    "用于收取邮件的服务器。网易系邮箱建议开启 IMAP ID。": {
      en: "Server used for incoming mail. NetEase mailboxes recommend enabling IMAP ID.",
    },
    "IMAP 主机": { en: "IMAP host" },
    "IMAP 端口": { en: "IMAP port" },
    "发送 IMAP ID（网易系建议）": { en: "Send IMAP ID (recommended for NetEase)" },
    "IMAP ID 信息": { en: "IMAP ID Info" },
    "部分邮箱服务商（如网易）要求客户端上报标识信息。": {
      en: "Some providers (e.g. NetEase) require the client to report identification info.",
    },
    "服务器同步": { en: "Server Sync" },
    "控制是否把本地的状态（已读/已回复/已删除/已发送 等）回写到 IMAP 服务器。如果服务器不支持子文件夹，会自动降级到上层文件夹。": {
      en: "Controls whether local state (read / replied / deleted / sent, etc.) is written back to the IMAP server. If subfolders are unsupported, it gracefully degrades to the parent folder.",
    },
    "全部同步": { en: "Sync All" },
    "全部不同步": { en: "Sync None" },
    "当前：未加载": { en: "Current: not loaded" },
    "✓ 当前：全部同步": { en: "✓ Current: sync all" },
    "🔒 当前处于完全不同步状态 — 任何状态都不会回写到邮件服务器": {
      en: "🔒 Currently fully un-synced — no local state is written back to the mail server",
    },
    "当前：自定义同步设置": { en: "Current: custom sync settings" },
    "同步已发送邮件（APPEND 到服务器 Sent）": {
      en: "Sync sent mail (APPEND to server Sent)",
    },
    "同步已读 / 已回复状态（\\Seen / \\Answered）": {
      en: "Sync read / replied state (\\Seen / \\Answered)",
    },
    "同步已删除（\\Deleted + EXPUNGE）": {
      en: "Sync deletes (\\Deleted + EXPUNGE)",
    },
    "同步文件夹结构（MOVE 至对应 mailbox，best-effort）": {
      en: "Sync folder structure (MOVE to matching mailbox, best-effort)",
    },
    "收取最近多少天的邮件": { en: "Fetch the most recent N days of mail" },
    "点击「收取邮件」时只拉取服务器上近 N 天的邮件（1–100 天）。 已经收过的邮件不会重复处理——按 IMAP UID 水位 + Message-Id 双重去重。": {
      en: "When fetching, only pull the last N days of mail from the server (1–100). Previously fetched mail is not reprocessed — deduplicated by IMAP UID watermark and Message-Id.",
    },
    "邮件落款": { en: "Email Signature" },
    "撰写新邮件或回复邮件时自动追加到正文末尾。每个账号可独立配置； 编辑草稿时不会重复追加（草稿里保存的是什么就用什么）。": {
      en: "Automatically appended to the body when composing or replying. Configured per account; not re-appended when editing drafts.",
    },
    "落款内容": { en: "Signature content" },
    "留空表示不追加落款。最多 2000 字符。": {
      en: "Leave empty to skip the signature. Max 2000 characters.",
    },
    "导入分类设置": { en: "Import Classification Settings" },
    "仅作用于当前账号": { en: "current account only" },
    "保存账号设置": { en: "Save Account Settings" },
    "关于这些按钮": { en: "About these buttons" },
    "连接诊断": { en: "Connection Diagnostics" },
    "已存配置": { en: "saved settings" },
    "测试激活账号已存的 SMTP / IMAP 连接": {
      en: "Test the active account's saved SMTP / IMAP connection",
    },
    "⌕ 连接诊断": { en: "⌕ Diagnostics" },
    "保存账号": { en: "Save Account" },
    "创建账号": { en: "Create Account" },
    "✚ 正在创建新账号": { en: "✚ Creating new account" },
    "已激活该账号": { en: "Account activated" },
    "账号已保存": { en: "Account saved" },
    "账号已创建": { en: "Account created" },
    "正在创建新账号": { en: "Creating new account" },
    "已开启全部同步并保存": { en: "All sync enabled and saved" },
    "已关闭全部同步并保存": { en: "All sync disabled and saved" },
    "已在表单中调整同步设置；点击「创建账号」后生效": {
      en: "Sync settings adjusted in the form; click \"Create Account\" to apply",
    },
    "诊断中…": { en: "Diagnosing…" },
    "诊断中，请稍候...": { en: "Diagnosing, please wait…" },
    "连接诊断通过：SMTP 与 IMAP 均可用": {
      en: "Diagnostics passed: SMTP and IMAP both working",
    },
    "连接诊断完成：请根据失败项提示修正配置": {
      en: "Diagnostics complete: please fix the failed items based on the messages",
    },
    "诊断失败": { en: "Diagnostics failed" },
    "已保存 DeepSeek API Key": { en: "DeepSeek API Key saved" },
    "API Key 不能为空": { en: "API Key cannot be empty" },
    "保存（代为编辑）": { en: "Save (admin edit)" },
    "代为编辑": { en: "admin edit" },
    "请先选择一个 prompts.json 文件。": {
      en: "Please choose a prompts.json file first.",
    },
    "请先选择一个 folders.json 文件。": {
      en: "Please choose a folders.json file first.",
    },
    "请先保存账号后再导入。": {
      en: "Please save the account before importing.",
    },
    "导入中…": { en: "Importing…" },
    "导入失败：": { en: "Import failed: " },
    "未知错误": { en: "Unknown error" },
    "导入 prompts.json": { en: "Import prompts.json" },
    "导入 folders.json": { en: "Import folders.json" },

    // Settings — placeholders / values shown in inputs
    "例如：工作邮箱 / 个人 Gmail": {
      en: "e.g., Work Mail / Personal Gmail",
    },
    "例如 you@example.com": { en: "e.g., you@example.com" },
    "授权码（非网页登录密码）": {
      en: "Authorization code (not the web login password)",
    },
    "例如 smtp.qq.com": { en: "e.g., smtp.qq.com" },
    "例如 imap.qq.com": { en: "e.g., imap.qq.com" },
    "已配置 · 输入新的 Key 以替换": {
      en: "Configured · enter a new Key to replace",
    },
    "加载失败：": { en: "Failed to load: " },
    "（仅管理员可修改）": { en: " (admin-only)" },
    "仅管理员可修改": { en: "Admin-only" },

    // ============ admin page ============
    "用户管理 · XEmail": { en: "User Management · XEmail" },
    "用户管理": { en: "User Management" },
    "管理员可在此创建普通/管理员用户、重置密码或删除用户。删除用户会一并删除其名下的所有邮箱账号。": {
      en: "Administrators can create normal/admin users, reset passwords, or delete users here. Deleting a user also deletes all of their email accounts.",
    },
    "系统模式": { en: "System Mode" },
    "开发态": { en: "Development" },
    "使用态": { en: "Production" },
    "后端服务": { en: "Backend Service" },
    "关闭后端": { en: "Shut Down Backend" },
    "新建用户": { en: "Create User" },
    "初始密码（至少 6 位）": { en: "Initial password (at least 6 characters)" },
    "普通用户": { en: "Normal User" },
    "管理员": { en: "Administrator" },
    "创建": { en: "Create" },
    "用户列表": { en: "User List" },
    "不能激活": { en: "Cannot activate" },
    "创建时间": { en: "Created at" },
    "邮件账号": { en: "Email accounts" },
    "操作": { en: "Actions" },
    "角色": { en: "Role" },
    "状态": { en: "Status" },
    "ID": { en: "ID" },
    "重置密码": { en: "Reset Password" },
    "删除用户": { en: "Delete User" },
    "确认删除": { en: "Confirm delete" },
    "确认": { en: "Confirm" },
    "无用户": { en: "No users" },
    "暂无用户": { en: "No users yet" },
    "XEmail · 用户管理": { en: "XEmail · User Management" },
    "系统模式": { en: "System Mode" },
    "📧 邮件账号": { en: "📧 Email Accounts" },
    "（我）": { en: " (me)" },
    "未登录": { en: "Not signed in" },
    "无权限": { en: "Forbidden" },
    "正在关闭…": { en: "Shutting down…" },
    "后端已关闭": { en: "Backend shut down" },
    "已发送关闭指令，后端将在数秒内停止。": {
      en: "Shutdown command sent. The backend will stop within a few seconds.",
    },
    "当前：开发态": { en: "Current: Development" },
    "当前：使用态": { en: "Current: Production" },
    "已切换到开发态：主页将显示调试按钮。": {
      en: "Switched to Development: debug buttons will appear on the home page.",
    },
    "已切换到使用态：主页将隐藏调试按钮。": {
      en: "Switched to Production: debug buttons are hidden on the home page.",
    },
    "没有用户": { en: "No users" },
    "不能删除自己": { en: "Cannot delete yourself" },
    "该用户当前激活": { en: "Currently active for this user" },
    "加载失败：": { en: "Failed to load: " },
    "密码至少 6 位": { en: "Password must be at least 6 characters" },
    "开发态会在主页显示「重新分类」「复位」等调试按钮；使用态隐藏它们，作为面向使用者的稳定版本。设置对所有用户即时生效。": {
      en: "Development mode shows debug buttons (\"Reclassify\", \"Reset\", etc.) on the home page; Production hides them. Applies to all users immediately.",
    },
    "点击后将停止本机的 XEmail 后端进程，所有用户的浏览器页面都会立刻无法访问。下次启动请双击项目目录下": {
      en: "Clicking will stop the local XEmail backend; all users' pages will be immediately unreachable. To restart, double-click",
    },
    "。": { en: "." },
    "点击「邮件账号 ▾」展开某个用户名下的邮箱账号。可代为编辑或删除，但管理员": {
      en: "Click \"Email Accounts ▾\" to expand a user's mailboxes. You can edit or delete them on the user's behalf, but admins",
    },
    "别人的邮箱账号——「激活」只能由账号的实际拥有者在自己的会话中执行。": {
      en: " others' mailboxes — \"Activate\" can only be done by the account's actual owner in their own session.",
    },

    // ============ contacts page ============
    "通讯录 · XEmail": { en: "Contacts · XEmail" },
    "通讯录": { en: "Contacts" },
    "+ 新建联系人": { en: "+ New Contact" },
    "新建联系人": { en: "New Contact" },
    "0 位联系人": { en: "0 contacts" },
    "搜索姓名 / 邮箱 / 备注 / 标签…": {
      en: "Search name / email / notes / tags…",
    },
    "搜索姓名 / 邮箱 / 标签…": {
      en: "Search name / email / tags…",
    },
    "用于在左侧\"标签\"栏快速筛选。最多 16 个，每个 ≤ 32 字符。": {
      en: "Used in the left-hand \"tags\" sidebar for quick filtering. Up to 16 tags, each ≤ 32 chars.",
    },
    "姓名": { en: "Name" },
    "邮箱": { en: "Email" },
    "备注": { en: "Notes" },
    "向此联系人发送新邮件": { en: "Send a new email to this contact" },
    "📇 通讯录": { en: "📇 Contacts" },
    "📇 打开通讯录管理 →": { en: "📇 Open Contacts Manager →" },
    "全部标签": { en: "All Tags" },
    "保存到通讯录": { en: "Save to Contacts" },
    "从通讯录选择": { en: "Choose from Contacts" },
    "例如：李戈": { en: "e.g., John Doe" },
    "多个标签用英文逗号分隔，例如：同事, 项目A": {
      en: "Separate multiple tags with commas, e.g., Coworker, Project A",
    },
    "可选：自由文字": { en: "Optional: free-form text" },
    "没有匹配的联系人。": { en: "No matching contacts." },
    "未激活": { en: "Not active" },
    "未分组": { en: "Untagged" },
    "通讯录为空，点击右上「+ 新建联系人」开始添加。": {
      en: "Contacts are empty. Click \"+ New Contact\" in the top right to start adding.",
    },
    "编辑联系人": { en: "Edit Contact" },
    "(未填写)": { en: "(not set)" },
    "请填写邮箱地址": { en: "Please enter an email address" },
    "已更新": { en: "Updated" },
    "已新建": { en: "Created" },
    "已删除": { en: "Deleted" },
    "位联系人": { en: " contacts" },
    "当前账号": { en: "Current account" },
    "的联系人。新增 / 编辑 / 删除均只影响该账号。": {
      en: "'s contacts. Adding / editing / deleting only affects this account.",
    },

    // ============ main inbox page (index.html) ============
    "尚未配置邮箱账号": { en: "No email account configured" },
    "→ 去设置": { en: "→ Go to Settings" },
    "⚙ 设置": { en: "⚙ Settings" },
    "👥 用户": { en: "👥 Users" },
    "全文": { en: "Full text" },
    "收发信人": { en: "Sender/Receiver" },
    "标题": { en: "Subject" },
    "主题": { en: "Subject" },
    "合并同源邮件": { en: "Merge same-thread emails" },
    "0 封邮件": { en: "0 emails" },
    "暂无邮件": { en: "No emails" },
    "请选择一封邮件查看内容": { en: "Select an email to view its content" },
    "使用说明": { en: "How to use" },
    "系统": { en: "System" },
    "由管理员维护；": { en: " maintained by administrators; " },
    "固定规则": { en: "Fixed Rules" },
    "命中即停；": { en: " evaluated first, short-circuit; " },
    "是用户用自然语言写的偏好；": {
      en: " are user preferences written in natural language; ",
    },
    "经验": { en: "Experience" },
    "新建子文件夹": { en: "New Subfolder" },
    "删除文件夹": { en: "Delete Folder" },
    "⚠ 需要配置 DeepSeek API Key": { en: "⚠ DeepSeek API Key required" },
    "请前往「设置」页填写一个有效的 API Key 后再继续。": {
      en: "Please go to Settings and provide a valid API Key before continuing.",
    },
    "前往设置": { en: "Go to Settings" },
    "为何重要？": { en: "Why is it important?" },
    "为什么归到这个分类？": { en: "Why this category?" },
    "撰写邮件": { en: "Compose" },
    "忽略": { en: "Discard" },
    "收件人": { en: "To" },
    "+ 抄送": { en: "+ Cc" },
    "+ 暗送": { en: "+ Bcc" },
    "抄送 (Cc)": { en: "Cc" },
    "所有收件人都能看到这些地址": {
      en: "All recipients can see these addresses",
    },
    "暗送 (Bcc)": { en: "Bcc" },
    "收件人 / 抄送人 看不到这些地址，但他们能收到": {
      en: "Recipients and Cc'd addresses can't see these, but they will receive the email",
    },
    "📝 来信摘要": { en: "📝 Incoming Summary" },
    "为帮助你撰写回复（仅英文邮件自动生成）": {
      en: "To help you draft a reply (auto-generated for English emails only)",
    },
    "正在阅读原邮件…": { en: "Reading the original email…" },
    "✨ 自动回复（实验性）": { en: "✨ Auto-Reply (experimental)" },
    "告诉系统你想回什么，由 DeepSeek 起草": {
      en: "Tell the system what you want to say; DeepSeek drafts it",
    },
    "✨ 自动生成（实验性）": { en: "✨ Auto-Compose (experimental)" },
    "告诉系统你要写什么，由 DeepSeek 起草": {
      en: "Tell the system what you want to write; DeepSeek drafts it",
    },
    "✨ 自动撰写转发备注（实验性）": {
      en: "✨ Auto-Forwarding Note (experimental)",
    },
    "告诉系统转发时想说什么，由 DeepSeek 起草": {
      en: "Tell the system what to add to the forward; DeepSeek drafts it",
    },
    "正文": { en: "Body" },
    "附件": { en: "Attachments" },
    "暂存": { en: "Save Draft" },
    "当前邮件还没发送，是否将其存入草稿箱？": {
      en: "This email has not been sent. Save it to drafts?",
    },
    "切换邮箱账号": { en: "Switch account" },
    "尚未配置任何邮箱账号，点击前往设置页添加": {
      en: "No email account configured. Click to go to Settings.",
    },
    "拉取本账号近 N 天内尚未收过的邮件（N 在「⚙ 设置 → 服务器同步 → 收取最近多少天的邮件」中配置）": {
      en: "Pull the last N days of unfetched emails for this account (N is set in Settings → Server Sync → fetch days)",
    },
    "对「未分类」文件夹中的邮件重新跑一遍分类（固定规则 + LLM）": {
      en: "Re-classify mail in the \"Uncategorized\" folder (fixed rules + LLM)",
    },
    "对当前账号下所有邮件（含已分类）重新跑一遍分类，不重新从服务器收取": {
      en: "Re-classify all mail under the current account (including already-classified), without re-fetching from the server",
    },
    "调试用：按服务器时间修正历史邮件时间（优先 INTERNALDATE）": {
      en: "Debug: fix historical email timestamps using server time (prefers INTERNALDATE)",
    },
    "调试用：清空当前账号已收到的邮件，方便重新收取测试": {
      en: "Debug: clear received mail for the current account so you can re-fetch for testing",
    },
    "显示/隐藏右侧智能分类设置面板": {
      en: "Show / hide the AI classification panel on the right",
    },
    "通讯录（新标签页打开）": { en: "Contacts (open in new tab)" },
    "设置（新标签页打开）": { en: "Settings (open in new tab)" },
    "用户管理（新标签页打开）": { en: "User Management (open in new tab)" },
    "拖动调整文件夹栏宽度（双击重置）": {
      en: "Drag to resize folder column (double-click to reset)",
    },
    "拖动调整邮件列表宽度（双击重置）": {
      en: "Drag to resize email list (double-click to reset)",
    },
    "拖动调整智能分类设置面板宽度（双击重置）": {
      en: "Drag to resize the classification panel (double-click to reset)",
    },
    "搜索范围": { en: "Search in" },
    "输入关键词搜索邮件": { en: "Search keywords in mail" },
    "清空": { en: "Clear" },
    "开启后将同源邮件合并为一条显示": {
      en: "When enabled, same-thread emails are merged into one row",
    },
    "设置：提交给大模型的邮件字段": {
      en: "Settings: fields submitted to the LLM",
    },
    "隐藏智能分类设置面板": { en: "Hide classification panel" },
    "例：发件人是导师，且收件人只有我，且主题包含明确截止日期。": {
      en: "e.g., Sender is my advisor, I am the only recipient, and subject has a hard deadline.",
    },
    "只切换标记，不记录经验": {
      en: "Just toggle the flag, don't record experience",
    },
    "例：来自财务系统的发票通知，主题包含「invoice / 账单」字样，应归入账单类。": {
      en: "e.g., Invoice notification from the finance system, subject contains \"invoice / 账单\", should go to the Bills folder.",
    },
    "只移动邮件，不记录经验": {
      en: "Just move the email, don't record experience",
    },
    "确认规则": { en: "Confirm Rule" },
    "名字 / 引用": { en: "Name / reference" },
    "你输入的规则": { en: "Your rule" },
    "AI 对它的理解": { en: "AI's interpretation" },
    "生成的程序（保存后由本地代码执行，不再调用大模型）": {
      en: "Generated program (after saving, executed locally without calling the LLM)",
    },
    "目标文件夹": { en: "Target folder" },
    "✉ 发送邮件": { en: "✉ Send Email" },
    "发件人": { en: "From" },
    "发件人:": { en: "From:" },
    "收件人:": { en: "To:" },
    "抄送:": { en: "Cc:" },
    "抄送": { en: "Cc" },
    "暗送": { en: "Bcc" },
    "暗送:": { en: "Bcc:" },
    "时间": { en: "Time" },
    "时间:": { en: "Time:" },
    "主题:": { en: "Subject:" },
    "发送时间": { en: "Sent at" },
    "发送时间:": { en: "Sent at:" },
    "删除时间:": { en: "Deleted at:" },
    "封": { en: "" },
    "封邮件": { en: " emails" },
    "已删除": { en: "Deleted" },
    "已置顶": { en: "Pinned" },
    "重要 · 已处理": { en: "Important · done" },
    "重要 · 待处理": { en: "Important · pending" },
    "重要待处理": { en: "Important pending" },
    "重要 (已处理)": { en: "Important (done)" },
    "★ 取消重要": { en: "★ Unmark Important" },
    "☆ 标为重要": { en: "☆ Mark Important" },
    "标为重要": { en: "Mark Important" },
    "取消重要": { en: "Unmark Important" },
    "✓ 已处理": { en: "✓ Done" },
    "🗑 删除": { en: "🗑 Delete" },
    "已标记为未读": { en: "Marked as unread" },
    "标记为已处理并移出「重要」": { en: "Mark as done and remove from \"Important\"" },
    "把所有收件人/抄送的人一起放入回复的收件人": {
      en: "Reply to all (To + Cc)",
    },
    "转发此邮件": { en: "Forward this email" },
    "提炼经验中…": { en: "Distilling experience…" },
    "移动并记录经验": { en: "Move and record experience" },
    "直接移动": { en: "Move only" },
    "已移至回收站": { en: "Moved to Trash" },
    "📌 置顶": { en: "📌 Pin" },
    "取消置顶": { en: "Unpin" },
    "确定继续吗？": { en: "Continue?" },
    "✎ 编辑 AST": { en: "✎ Edit AST" },
    "生成中…": { en: "Generating…" },
    "已生成。请检查后再点「发送」。": {
      en: "Generated. Please review before clicking \"Send\".",
    },
    "编辑草稿": { en: "Edit Draft" },
    "该账号": { en: "This account" },
    "当前账号": { en: "Current account" },
    "-------- 转发邮件 --------": { en: "-------- Forwarded message --------" },
    "-------- 原邮件 --------": { en: "-------- Original message --------" },
    "原邮件": { en: "Original message" },
    "转发邮件": { en: "Forwarded message" },
    "未读": { en: "Unread" },
    "已读": { en: "Read" },
    "纯文本": { en: "Plain text" },
    "致": { en: "to" },
    "其他人": { en: "others" },
    "新增标签…": { en: "Add tag…" },
    "新增联系人": { en: "Add Contact" },
    "邮件正文": { en: "Email body" },
    "邮件主题": { en: "Email subject" },
    "邮件标签": { en: "Email tags" },
    "附件文件名": { en: "Attachment filename" },
    "请填理由（必填）。可写明：哪类邮件、判定条件、为什么算「重要」。 请尽量精炼且明确，会作为长期偏好，自动应用到后续邮件分类中。": {
      en: "Please enter a reason (required). State: what type of mail, the conditions, why it counts as \"important\". Keep it precise and concrete — it will become a long-term preference applied to future classifications.",
    },
    "请说明该邮件应归入哪个文件夹的原因（必填）。可写明： 1) 邮件特征（发件人 / 主题 / 关键词 / 收件人模式等）； 2) 归类逻辑。建议精炼且具体，将作为长期偏好用于后续分类。": {
      en: "Please explain why this email belongs to the target folder (required). State: 1) email characteristics (sender / subject / keywords / recipient pattern, etc.); 2) the classification logic. Be precise and concrete — it will become a long-term preference for future classification.",
    },
    "新邮件，请尽快查阅。": { en: "New email — please review." },
    "无主题邮件": { en: "Email without subject" },
    "（已跳过": { en: "(skipped " },
    "封；详见服务器日志）": { en: " emails; see server logs)" },

    // ============ language selector card ============
    "界面语言": { en: "Interface Language" },
    "选择 XEmail 界面的显示语言。": {
      en: "Choose the language used throughout XEmail.",
    },
    "已切换为中文": { en: "Switched to Chinese" },
    "已切换为英文": { en: "Switched to English" },
    "已更新固定规则": { en: "Fixed rule updated" },
    "已添加固定规则": { en: "Fixed rule added" },
    "已删除经验": { en: "Experience deleted" },
    "已添加经验": { en: "Experience added" },
    "经验内容不能为空": { en: "Experience content cannot be empty" },
    "请填写经验内容": { en: "Please enter experience content" },
    "提示内容不能为空": { en: "Prompt content cannot be empty" },
    "请填写提示内容": { en: "Please enter prompt content" },
    "已添加提示": { en: "Prompt added" },
    "系统提示已保存": { en: "System prompt saved" },
    "已恢复为内置默认": { en: "Restored to built-in default" },
    "已保存提交字段设置": { en: "Submitted field settings saved" },
    "请先用自然语言描述规则": { en: "Please describe the rule in natural language first" },
    "请选择目标文件夹": { en: "Please choose a target folder" },
    "解析中…": { en: "Parsing…" },
    "删除这条固定规则？": { en: "Delete this fixed rule?" },
    "删除这条经验？": { en: "Delete this experience?" },
    "删除这条提示？": { en: "Delete this prompt?" },
    "恢复到内置默认提示？当前的自定义内容会被覆盖。": {
      en: "Restore to built-in default prompt? Your custom content will be overwritten.",
    },
    "正文加载失败：": { en: "Failed to load body: " },
    "发送": { en: "Send" },
    "发送中…": { en: "Sending…" },
    "发送失败": { en: "Send failed" },
    "已发送": { en: "Sent" },
    "已暂存": { en: "Saved as draft" },
    "（未填写）": { en: "(not set)" },
    "（不限）": { en: "(any)" },
    "未填写": { en: "not set" },
    "拉取中…": { en: "Fetching…" },
    "重新分类": { en: "Reclassify" },
    "重新分类全部": { en: "Reclassify All" },
    "复位": { en: "Reset" },
    "全部分类中…": { en: "Reclassifying all…" },
    "分类中…": { en: "Classifying…" },
    "刷新中…": { en: "Refreshing…" },
    "正在加载邮件…": { en: "Loading emails…" },
    "已收取": { en: "Fetched" },
    "无新邮件": { en: "No new emails" },
    "拉取失败": { en: "Fetch failed" },
    "复位失败": { en: "Reset failed" },
    "本账号已复位": { en: "This account has been reset" },
    "确定": { en: "OK" },
    "确认": { en: "Confirm" },
    "取消": { en: "Cancel" },
    "保存": { en: "Save" },
    "已添加": { en: "Added" },
    "已修改": { en: "Modified" },
    "新建": { en: "New" },
    "草稿": { en: "Draft" },
    "草稿箱": { en: "Drafts" },
    "回收站": { en: "Trash" },
    "收件箱": { en: "Inbox" },
    "保存为草稿": { en: "Save as draft" },
    "保存中…": { en: "Saving…" },
    "已保存到草稿箱": { en: "Saved to drafts" },
    "确定丢弃当前邮件吗？": { en: "Discard the current email?" },
    "已丢弃": { en: "Discarded" },
    "撰写时间": { en: "Composed at" },
    "未读邮件": { en: "Unread emails" },
    "封新邮件": { en: " new emails" },
    "封邮件": { en: " emails" },
    "无邮件": { en: "No emails" },
    "邮件正文": { en: "Email body" },
    "邮件主题": { en: "Email subject" },
    "邮件标签": { en: "Email tags" },
    "未连接": { en: "Disconnected" },
    "已连接": { en: "Connected" },
    "确认保存": { en: "Confirm Save" },
    "收起编辑": { en: "Collapse editor" },
    "JSON 解析失败：": { en: "JSON parse failed: " },
    "✓ AST 校验通过，伪代码已刷新": { en: "✓ AST validated; pseudocode refreshed" },
    "校验请求失败：": { en: "Validation request failed: " },
    "为何取消重要？": { en: "Why unmark important?" },
    "直接标为重要": { en: "Just mark important" },
    "直接取消重要": { en: "Just unmark important" },
    "执行中…": { en: "Running…" },
    "移动中…": { en: "Moving…" },
    "清理中…": { en: "Cleaning…" },
    "修正中…": { en: "Fixing…" },
    "收取中…": { en: "Fetching…" },
    "正在同步…": { en: "Syncing…" },
    "重新分类中…": { en: "Reclassifying…" },
    "(模型未返回摘要)": { en: "(model returned no summary)" },
    "摘要生成失败：": { en: "Failed to generate summary: " },
    "正在请求 DeepSeek 起草邮件…": { en: "Requesting DeepSeek to draft email…" },
    "时间修正：将对当前账号已收取的历史邮件，按服务器时间回填 received_at。": {
      en: "Time fix: will backfill received_at for already-fetched historical emails using server time.",
    },
    "正文加载失败": { en: "Failed to load body" },

    // ============ index.html toolbar / sidebar ============
    "撰写": { en: "Compose" },
    "收取邮件": { en: "Fetch Mail" },
    "🧠 执行分类": { en: "🧠 Classify" },
    "🔄 重新分类": { en: "🔄 Reclassify" },
    "🕒 修正时间": { en: "🕒 Fix Times" },
    "◧ 智能分类设置": { en: "◧ Classification Settings" },
    "智能分类设置": { en: "Classification Settings" },
    "搜索": { en: "Search" },
    "搜索范围": { en: "Search in" },
    "邮件搜索范围": { en: "Mail search scope" },
    "邮件搜索关键词": { en: "Mail search keywords" },
    "清空搜索": { en: "Clear search" },
    "全文": { en: "Full text" },
    "收发信人": { en: "Sender/Receiver" },
    "标题": { en: "Subject" },
    "合并同源邮件": { en: "Merge thread" },
    "0 封邮件": { en: "0 emails" },
    "暂无邮件": { en: "No emails" },
    "请选择一封邮件查看内容": { en: "Select an email to view its content" },

    // ============ classification settings panel ============
    "使用说明": { en: "How to use" },
    "指导 DeepSeek 对新邮件做分类与重要性判断。下方四类可独立折叠：": {
      en: "Guides DeepSeek to classify new emails and judge importance. The four sections below can be collapsed independently:",
    },
    "由管理员维护；": { en: " maintained by administrators; " },
    "命中即停；": { en: " evaluated first, short-circuit; " },
    "是用户用自然语言写的偏好；": {
      en: " are user preferences written in natural language; ",
    },
    "是从「为何重要 / 为何取消重要」对话中沉淀的判例。": {
      en: " are case rulings distilled from the \"why important / unmark important\" dialogs.",
    },
    "正在加载提交字段…": { en: "Loading submitted fields…" },
    "设置：提交给大模型的邮件字段": {
      en: "Settings: fields submitted to the LLM",
    },
    "隐藏智能分类设置面板": { en: "Hide classification panel" },
    "显示/隐藏右侧智能分类设置面板": {
      en: "Show / hide the classification panel on the right",
    },
    "拖动调整智能分类设置面板宽度（双击重置）": {
      en: "Drag to resize the classification panel (double-click to reset)",
    },

    // ============ banner / modals ============
    "⚠ 需要配置 DeepSeek API Key": { en: "⚠ DeepSeek API Key required" },
    "智能分类依赖后端的大模型（DeepSeek V4）。系统检测到当前尚未配置 API Key，分类功能将不可用，所有新邮件会被标记为「未分类」。": {
      en: "Smart classification depends on the backend LLM (DeepSeek V4). No API Key is configured, so classification is disabled and all new emails will be marked \"Uncategorized\".",
    },
    "请前往「设置」页填写一个有效的 API Key 后再继续。": {
      en: "Please go to Settings and provide a valid API Key before continuing.",
    },
    "稍后再说": { en: "Later" },
    "前往设置": { en: "Go to Settings" },

    // ============ importance / recategorize modals ============
    "为何重要？": { en: "Why is it important?" },
    "为何取消重要？": { en: "Why unmark important?" },
    "请说明判断理由。系统会结合邮件内容把它整理成一条简短经验，存入「经验」类，影响以后类似邮件的判断。": {
      en: "Please explain your reasoning. The system will distill it into a short experience and store it under \"Experience\", affecting future judgments on similar emails.",
    },
    "直接执行": { en: "Just do it" },
    "为什么归到这个分类？": { en: "Why this category?" },
    "请说明这封邮件为何应归入此分类。系统会结合邮件内容把它整理成一条简短经验，存入「经验」类，影响以后类似邮件的判断。": {
      en: "Please explain why this email belongs here. The system will distill it into a short experience and store it under \"Experience\", affecting future judgments on similar emails.",
    },

    // ============ contacts modal in inbox ============
    "保存到通讯录": { en: "Save to Contacts" },
    "邮箱地址不可在此修改。如需修改请前往": {
      en: "Email address cannot be modified here. To change it, please go to ",
    },
    "✉ 发送邮件": { en: "✉ Send Email" },
    "全部标签": { en: "All Tags" },
    "📇 打开通讯录管理 →": { en: "📇 Open Contacts Manager →" },
    "添加选中": { en: "Add Selected" },

    // ============ rule preview / AST modal ============
    "确认规则": { en: "Confirm Rule" },
    "名字 / 引用": { en: "Name / reference" },
    "你输入的规则": { en: "Your rule" },
    "AI 对它的理解": { en: "AI's interpretation" },
    "生成的程序（保存后由本地代码执行，不再调用大模型）": {
      en: "Generated program (after saving, executed locally without calling the LLM)",
    },
    "✎ 编辑 AST": { en: "✎ Edit AST" },
    "校验并刷新预览": { en: "Validate & refresh preview" },
    "撤销编辑": { en: "Undo Edit" },
    "目标文件夹": { en: "Target folder" },
    "确认保存": { en: "Confirm Save" },

    // ============ compose modal ============
    "撰写邮件": { en: "Compose" },
    "携带": { en: "with" },
    "忽略": { en: "discard" },
    "+ 抄送": { en: "+ Cc" },
    "+ 暗送": { en: "+ Bcc" },
    "抄送 (Cc)": { en: "Cc" },
    "暗送 (Bcc)": { en: "Bcc" },
    "所有收件人都能看到这些地址": {
      en: "All recipients can see these addresses",
    },
    "收件人 / 抄送人 看不到这些地址，但他们能收到": {
      en: "Recipients and Cc'd addresses can't see these, but they will receive the email",
    },
    "显示 / 隐藏抄送": { en: "Show / hide Cc" },
    "显示 / 隐藏暗送（其他收件人不可见）": {
      en: "Show / hide Bcc (hidden from other recipients)",
    },
    "📝 来信摘要": { en: "📝 Incoming Summary" },
    "为帮助你撰写回复（仅英文邮件自动生成）": {
      en: "To help you draft a reply (auto-generated for English emails only)",
    },
    "✨ 自动回复（实验性）": { en: "✨ Auto-Reply (experimental)" },
    "告诉系统你想回什么，由 DeepSeek 起草": {
      en: "Tell the system what you want to say; DeepSeek drafts it",
    },
    "✨ 生成回复": { en: "✨ Draft Reply" },
    "✨ 自动生成（实验性）": { en: "✨ Auto-Compose (experimental)" },
    "告诉系统你要写什么，由 DeepSeek 起草": {
      en: "Tell the system what you want to write; DeepSeek drafts it",
    },
    "✨ 生成正文": { en: "✨ Draft Body" },
    "✨ 自动撰写转发备注（实验性）": {
      en: "✨ Auto-Forwarding Note (experimental)",
    },
    "告诉系统转发时想说什么，由 DeepSeek 起草": {
      en: "Tell the system what to add to the forward; DeepSeek drafts it",
    },
    "✨ 生成转发备注": { en: "✨ Draft Forwarding Note" },
    "正文": { en: "Body" },
    "附件": { en: "Attachments" },
    "暂存": { en: "Save Draft" },
    "丢弃": { en: "Discard" },
    "存入草稿": { en: "Save to Drafts" },
    "当前邮件还没发送，是否将其存入草稿箱？": {
      en: "This email has not been sent. Save it to drafts?",
    },
    "someone@example.com（多个用英文逗号分隔）": {
      en: "someone@example.com (separate multiple with commas)",
    },
    "cc@example.com（多个用英文逗号分隔）": {
      en: "cc@example.com (separate multiple with commas)",
    },
    "bcc@example.com（多个用英文逗号分隔）": {
      en: "bcc@example.com (separate multiple with commas)",
    },
    "请输入主题": { en: "Enter subject" },
    "请输入正文内容...": { en: "Enter body…" },
    "例：礼貌确认收到、同意周三下午开会，并请对方发来议程。": {
      en: "e.g., Politely confirm receipt, agree to meet Wednesday afternoon, and ask for the agenda.",
    },
    "例：给王老师写邮件请教稀疏注意力机制的论文，并约下周一对一讨论。": {
      en: "e.g., Ask Prof. Wang about a paper on sparse attention and propose a 1:1 next week.",
    },
    "例：转给王老师一阅，请确认会议时间能否参加。": {
      en: "e.g., Forwarding to Prof. Wang for review; please confirm whether you can attend the meeting.",
    },

    // ============ settings.html paragraph chunks split by inline elements ============
    "从备份或另一台机器导出的": {
      en: "From a backup or another machine's exported ",
    },
    "文件还原本账号的智能分类设置和文件夹结构。": {
      en: " file, restore this account's classification settings and folder structure.",
    },
    "导入会替换当前账号的对应数据": {
      en: "Import will replace this account's existing data",
    },
    "（其它账号、全局系统提示均不受影响），不可撤销，请先备份。 导入时会自动把文件里的": {
      en: " (other accounts and the global system prompt are not affected); this cannot be undone — please back up first. The file's ",
    },
    "重新指向当前账号，所以无需担心来源机器的账号 ID 与本机不一致。": {
      en: " will be remapped to the current account, so don't worry about ID mismatches between machines.",
    },
    "prompts.json（用户提示 / 固定规则 / 经验 / 字段配置）": {
      en: "prompts.json (user prompts / fixed rules / experience / field config)",
    },
    "folders.json（本账号的文件夹列表）": {
      en: "folders.json (this account's folder list)",
    },
    "：把上方表单中的所有改动写入。新增账号时会同时把它设为「激活账号」。": {
      en: ": writes all the changes above. When creating a new account, it is also set as the \"active account\".",
    },
    "：丢弃本次改动，返回账号列表（等同于顶部「← 返回」）。": {
      en: ": discards changes and returns to the account list (same as \"← Back\" at the top).",
    },
    "：用当前「激活账号」的": {
      en: ": uses the current \"active account\"'s ",
    },
    "测试 SMTP / IMAP 是否可登录；如果你刚改了表单但还没点保存，诊断不会用到这些新值，请先保存再诊断。": {
      en: "to test whether SMTP / IMAP login works; if you changed the form but haven't saved yet, the diagnostic won't use the new values — please save first.",
    },
    "例如：&#10;&#10;此致&#10;敬礼&#10;张三 · 示例科技有限公司": {
      en: "e.g.,&#10;&#10;Best regards,&#10;&#10;John Doe · Example Co., Ltd.",
    },

    // ============ contacts.html missing entries ============
    "用于在左侧\"标签\"栏快速筛选。最多 16 个，每个 ≤ 32 字符。": {
      en: "Used in the left-hand \"tags\" sidebar for quick filtering. Up to 16 tags, each ≤ 32 chars.",
    },

    // ============ index.html — dynamic JS strings (frequent atoms) ============
    "工作": { en: "Work" },
    "账单": { en: "Bills" },
    "营销": { en: "Marketing" },
    "社交": { en: "Social" },
    "垃圾邮件": { en: "Spam" },
    "（管理员）": { en: " (admin)" },
    "(无描述)": { en: "(no description)" },
    "(将自动生成)": { en: "(auto-generated)" },
    "解析": { en: "Parse" },
    "重新解析": { en: "Re-parse" },
    "拖动调整顺序": { en: "Drag to reorder" },
    "上移（优先级提高）": { en: "Move up (higher priority)" },
    "下移（优先级降低）": { en: "Move down (lower priority)" },
    "跳过 LLM，直接编辑生成的 AST": {
      en: "Skip LLM and edit the generated AST directly",
    },
    "将更新原有规则（旧的程序会被替换）": {
      en: "Will update the existing rule (the old program will be replaced)",
    },
    "将新增一条固定规则": { en: "Will add a new fixed rule" },
    "正文（前 ${cap} 字符）": { en: "Body (first ${cap} chars)" },
    "正文（已禁用内容）": { en: "Body (content disabled)" },
    "当前未提交任何字段（仅靠系统提示判断）": {
      en: "No fields are submitted (judgment relies on the system prompt only)",
    },
    "例：发件人后缀是 @example.com，或者主题里含「发票」的，归到 工作。也可在描述中用 @{name} 引用其他规则或提示。": {
      en: "e.g., Sender's domain is @example.com, or subject contains \"invoice\" → Work. You can also reference other rules/prompts with @{name}.",
    },
    "显示右侧智能分类设置面板": { en: "Show the classification panel" },
    "隐藏右侧智能分类设置面板": { en: "Hide the classification panel" },
    "已生成。请检查后再点「发送」。": {
      en: "Generated. Please review before clicking \"Send\".",
    },
    "正在请求 DeepSeek 起草回复…": {
      en: "Requesting DeepSeek to draft a reply…",
    },
    "正在请求 DeepSeek 起草转发备注…": {
      en: "Requesting DeepSeek to draft a forwarding note…",
    },
    "+ 添加附件": { en: "+ Add Attachment" },
    "编辑草稿": { en: "Edit Draft" },
    "草稿已自动保存": { en: "Draft auto-saved" },
    "草稿已保存": { en: "Draft saved" },
    "AST 不合法：": { en: "AST invalid: " },
    "(当前)": { en: " (current)" },
    "没有匹配的名字": { en: "No matching names" },
    "重要 · 已处理": { en: "Important · Done" },
    "重要 · 待处理": { en: "Important · Pending" },
    "重要待处理": { en: "Important Pending" },
    "重要 (已处理)": { en: "Important (Done)" },
    "★ 取消重要": { en: "★ Unmark Important" },
    "☆ 标为重要": { en: "☆ Mark Important" },
    "✓ 已处理": { en: "✓ Done" },
    "🗑 删除": { en: "🗑 Delete" },
    "📌 置顶": { en: "📌 Pin" },
    "取消置顶": { en: "Unpin" },
    "已置顶": { en: "Pinned" },
    "已标记为未读": { en: "Marked as unread" },
    "已移至回收站": { en: "Moved to Trash" },
    "把所有收件人/抄送的人一起放入回复的收件人": {
      en: "Reply to all (To + Cc)",
    },
    "转发此邮件": { en: "Forward this email" },
    "请填写理由后再确认": { en: "Please enter a reason before confirming" },
    "标记为已处理并移出「重要」": { en: "Mark as done and remove from \"Important\"" },
    "(无主题)": { en: "(No subject)" },
    "(无正文)": { en: "(No body)" },
    "(未命名邮件)": { en: "(Unnamed email)" },
    "未命名": { en: "Unnamed" },
    "未命名账号": { en: "Unnamed account" },
    "发件人:": { en: "From:" },
    "收件人:": { en: "To:" },
    "抄送:": { en: "Cc:" },
    "暗送:": { en: "Bcc:" },
    "时间:": { en: "Time:" },
    "发送时间:": { en: "Sent at:" },
    "删除时间:": { en: "Deleted at:" },
    "主题:": { en: "Subject:" },
    "-------- 转发邮件 --------": { en: "-------- Forwarded message --------" },
    "-------- 原邮件 --------": { en: "-------- Original message --------" },
    "原邮件": { en: "Original message" },
    "转发邮件": { en: "Forwarded message" },
    "致": { en: "to" },
    "其他人": { en: "others" },
    "曾经重要": { en: "Previously Important" },
    "纯文本": { en: "Plain text" },
    "封": { en: "" },
    "名字（可留空自动生成。字母/数字/_/-, 1-48 字符）": {
      en: "Name (optional — auto-generated. letters/digits/_/-, 1-48 chars)",
    },
    "附件文件名": { en: "Attachment filename" },
    "新增标签…": { en: "Add tag…" },
    "新增联系人": { en: "Add Contact" },
    "邮件正文": { en: "Email body" },
    "邮件主题": { en: "Email subject" },
    "邮件标签": { en: "Email tags" },

    // ============ classification settings panel — dynamic sections ============
    // Section: Fixed Rules
    "⚙ 固定规则": { en: "⚙ Fixed Rules" },
    "规则按从上到下顺序匹配，命中即停；规则整体优先于下方提示。 用自然语言描述，AI 翻译成程序后由本地代码执行，不再调用大模型。": {
      en: "Rules are matched top-to-bottom and short-circuit on the first hit; fixed rules outrank prompts below. Describe in natural language; the AI translates it into a program that runs locally without further LLM calls.",
    },
    "✎ 正在重新解析：": { en: "✎ Re-parsing: " },
    "取消编辑": { en: "Cancel edit" },
    "规则按上下顺序优先级判定；输入": {
      en: "Rules are prioritized by their order; type ",
    },
    "可触发名字补全；保存时检测循环引用。": {
      en: " to trigger name completion; saves are checked for cyclic references.",
    },
    "改写描述": { en: "Edit description" },
    "✎ 编辑程序": { en: "✎ Edit program" },
    "查看生成的程序": { en: "View generated program" },
    "作者:": { en: "Author:" },
    "名字（可留空自动生成）": { en: "Name (optional, auto-generated)" },

    // Section: System Prompt
    "默认": { en: "Default" },
    "已自定义": { en: "Customized" },
    "仅管理员可改": { en: "Admin-only" },
    "📝 系统提示": { en: "📝 System Prompt" },
    "清空覆盖、回到内置默认": {
      en: "Clear overrides and revert to the built-in default",
    },
    "恢复默认": { en: "Restore Default" },
    "保存系统提示": { en: "Save System Prompt" },

    // Section: Prompts
    "💬 提示": { en: "💬 Prompts" },
    "还没有自定义提示。在下方「添加提示」加一条。": {
      en: "No custom prompts yet. Add one below in \"Add Prompt\".",
    },
    "应用于:": { en: "Apply to:" },
    "此提示的名字": { en: "Name of this prompt" },
    "提示": { en: "Prompts" },
    "＋ 添加提示": { en: "＋ Add Prompt" },
    "添加": { en: "Add" },
    "例如：主题含「发票 / 报销」的邮件归入「账单」。优先级低于上方固定规则。": {
      en: "e.g., Emails whose subject contains \"invoice / reimbursement\" → Bills. Lower priority than fixed rules above.",
    },

    // Section: Experiences
    "💡 经验": { en: "💡 Experience" },
    "点击「☆ 标为重要」或「★ 取消重要」时，系统会请你说明理由并把它沉淀为一条经验。 你也可以在下方手动添加 / 修改 / 删除经验。所有经验都会作为通用指引发给大模型。": {
      en: "When you click \"☆ Mark Important\" or \"★ Unmark Important\", the system asks for a reason and distills it into an experience. You can also add / edit / delete experiences manually below. All experiences are passed to the LLM as general guidance.",
    },
    "还没有积累经验。从邮件详情页打/取消「重要」标记开始吧。": {
      en: "No experiences yet. Start by toggling \"Important\" on an email from the detail view.",
    },
    "★ 标为重要": { en: "★ Marked Important" },
    "☆ 取消重要": { en: "☆ Unmarked Important" },
    "手工": { en: "Manual" },
    "＋ 添加经验（手工）": { en: "＋ Add Experience (manual)" },
    "例：来自 @example.com 的群发邮件不应被标为重要。": {
      en: "e.g., Bulk mail from @example.com should not be marked important.",
    },

    // Section: Field config editor (submit-fields settings)
    "📤 提交给大模型的字段": { en: "📤 Fields submitted to the LLM" },
    "正文截取前": { en: "Body first" },
    "字符（0 表示不发正文内容）": {
      en: " chars (0 = don't submit body content)",
    },
    "当前提交：": { en: "Submitting: " },

    // Field labels (referenced by FIELD_LABELS at render time via t())
    "发件人": { en: "From" },
    "收件人": { en: "To" },
    "主题": { en: "Subject" },
    "正文": { en: "Body" },
    "附件文件名": { en: "Attachment filename" },
  };

  function getLang() {
    try {
      var v = localStorage.getItem(STORAGE_KEY);
      return v === "en" ? "en" : "zh";
    } catch (_) {
      return "zh";
    }
  }

  function setLang(lang) {
    lang = lang === "en" ? "en" : "zh";
    try {
      localStorage.setItem(STORAGE_KEY, lang);
    } catch (_) {}
    apply();
    fireChange(lang);
  }

  // Translate a known Chinese key. Returns the original key if no entry
  // exists or the current language is Chinese.
  function t(key) {
    if (key == null) return key;
    var lang = getLang();
    if (lang === "zh") return key;
    var entry = DICT[key];
    if (entry && entry[lang]) return entry[lang];
    // Try a whitespace-normalized lookup so callers don't have to worry
    // about leading/trailing spaces or embedded source-formatting whitespace.
    var nk = ("" + key).replace(/\s+/g, " ").trim();
    var e2 = DICT[nk];
    if (e2 && e2[lang]) return e2[lang];
    return key;
  }

  // Format helper: takes a zh template string + vars and applies them.
  // Markers use ${name} so callers can pass either a plain string template
  // or pre-interpolated text (in which case it round-trips unchanged).
  t.fmt = function (key, vars) {
    var s = t(key);
    if (vars && typeof s === "string") {
      Object.keys(vars).forEach(function (k) {
        s = s.split("${" + k + "}").join(String(vars[k]));
      });
    }
    return s;
  };

  // ============ DOM walker ============

  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, CODE: 1, PRE: 1 };

  // Cache of original (zh) text per node so we can revert. We can't use a
  // plain object because we want per-node identity; WeakMap keeps memory
  // bounded as nodes get removed.
  var textOrig = new WeakMap();
  var attrOrig = new WeakMap();
  var titleOrig = null;

  function normalizeKey(s) {
    // Collapse runs of whitespace (newlines + indentation in HTML source)
    // into single spaces so dictionary lookups can ignore source formatting.
    return ("" + s).replace(/\s+/g, " ").trim();
  }

  function translateTextNode(node, lang) {
    var original = textOrig.get(node);
    if (original === undefined) {
      original = node.nodeValue;
      // Only cache if it's a viable candidate (contains some non-whitespace).
      if (!/\S/.test(original)) return;
      var key = normalizeKey(original);
      if (!DICT[key]) return; // not translatable
      textOrig.set(node, original);
    }
    if (lang === "zh") {
      node.nodeValue = original;
    } else {
      var key2 = normalizeKey(original);
      var entry = DICT[key2];
      if (entry && entry[lang]) {
        var lead = original.match(/^\s*/)[0];
        var trail = original.match(/\s*$/)[0];
        node.nodeValue = lead + entry[lang] + trail;
      } else {
        node.nodeValue = original;
      }
    }
  }

  function translateAttr(el, attr, lang) {
    var origMap = attrOrig.get(el);
    if (!origMap) {
      origMap = {};
      attrOrig.set(el, origMap);
    }
    if (!(attr in origMap)) {
      var cur = el.getAttribute(attr);
      if (cur == null) return;
      if (!DICT[normalizeKey(cur)]) return;
      origMap[attr] = cur;
    }
    var original = origMap[attr];
    if (lang === "zh") {
      el.setAttribute(attr, original);
    } else {
      var entry = DICT[normalizeKey(original)];
      if (entry && entry[lang]) {
        var lead = original.match(/^\s*/)[0];
        var trail = original.match(/\s*$/)[0];
        el.setAttribute(attr, lead + entry[lang] + trail);
      } else {
        el.setAttribute(attr, original);
      }
    }
  }

  function walkText(root, lang) {
    if (!root) return;
    var walker = document.createTreeWalker(
      root,
      NodeFilter.SHOW_TEXT,
      {
        acceptNode: function (node) {
          if (node.parentElement && SKIP_TAGS[node.parentElement.tagName]) {
            return NodeFilter.FILTER_REJECT;
          }
          return NodeFilter.FILTER_ACCEPT;
        },
      },
      false
    );
    var nodes = [];
    var n;
    while ((n = walker.nextNode())) nodes.push(n);
    for (var i = 0; i < nodes.length; i++) translateTextNode(nodes[i], lang);
  }

  function walkAttrs(root, lang) {
    if (!root || !root.querySelectorAll) return;
    var ATTRS = ["placeholder", "title", "alt", "aria-label", "value"];
    var sel = ATTRS.map(function (a) {
      return "[" + a + "]";
    }).join(",");
    var nodes = root.querySelectorAll(sel);
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      // Skip <input value="..."> for inputs the user types into; only
      // translate value on button/submit-style elements where it's a label.
      for (var j = 0; j < ATTRS.length; j++) {
        var a = ATTRS[j];
        if (!el.hasAttribute(a)) continue;
        if (a === "value") {
          var tag = el.tagName;
          var typ = (el.getAttribute("type") || "").toLowerCase();
          if (
            tag !== "BUTTON" &&
            !(tag === "INPUT" && (typ === "button" || typ === "submit" || typ === "reset"))
          )
            continue;
        }
        translateAttr(el, a, lang);
      }
    }
  }

  function translateTitle(lang) {
    if (titleOrig === null) titleOrig = document.title;
    var entry = DICT[normalizeKey(titleOrig)];
    if (lang !== "zh" && entry && entry[lang]) {
      document.title = entry[lang];
    } else {
      document.title = titleOrig;
    }
  }

  function apply() {
    var lang = getLang();
    document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    walkText(document.body, lang);
    walkAttrs(document.body, lang);
    translateTitle(lang);
  }

  // ============ change listeners ============
  var listeners = [];
  function onChange(fn) {
    listeners.push(fn);
  }
  function fireChange(lang) {
    for (var i = 0; i < listeners.length; i++) {
      try {
        listeners[i](lang);
      } catch (_) {}
    }
  }

  // ============ public API ============
  global.I18n = {
    getLang: getLang,
    setLang: setLang,
    t: t,
    apply: apply,
    onChange: onChange,
    DICT: DICT, // exposed for callers that want to extend / inspect
  };

  // Expose t() at the top level too for terse calls in pages.
  global.t = t;

  // Auto-apply once the DOM is ready. Pages that build content dynamically
  // should call I18n.apply() again after insertion.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", apply);
  } else {
    apply();
  }

  // Cross-tab sync: when the language is changed in /settings, other open
  // tabs (inbox, contacts, admin) should re-translate without a manual
  // reload. The `storage` event fires only in *other* tabs sharing the same
  // localStorage origin, so this complements (doesn't loop with) setLang.
  global.addEventListener("storage", function (e) {
    if (e.key !== STORAGE_KEY) return;
    apply();
    fireChange(getLang());
  });
})(window);
