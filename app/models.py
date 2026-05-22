# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class EmailSettings(BaseModel):
    sender_email: EmailStr
    sender_password: str = Field(..., min_length=1)
    receiver_email: EmailStr
    smtp_host: str
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    smtp_use_starttls: bool = False
    imap_host: str
    imap_port: int = 993
    imap_use_ssl: bool = True
    imap_send_id: bool = True
    imap_id_name: str = "XEmail"
    imap_id_version: str = "0.1.0"
    imap_id_vendor: str = "XEmail"
    imap_id_support_email: EmailStr = "support@example.com"
    # Free-form signature appended to the body of new emails and replies
    # (not draft edits — drafts already carry whatever the user saved).
    # Empty by default; per-account so each mailbox can have its own.
    signature: str = Field(default="", max_length=2000)


class Attachment(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"
    size: int = 0


class SendEmailRequest(BaseModel):
    # Free-form, comma-separated address list to support Reply-All-style
    # multi-recipient sends. Pydantic's EmailStr only validates a single
    # address; we parse + validate per-address in the route. Each of `to`,
    # `cc`, `bcc` accepts the same format ("Name <addr>, addr2, …").
    # Bcc is honored at SMTP RCPT-TO level but the Bcc header is NOT
    # written into the message, so To/Cc recipients can't see Bcc'd
    # addresses — standard mail-client semantics.
    to: str
    cc: str = ""
    bcc: str = ""
    subject: str = Field(default="")
    body: str = Field(default="")
    draft_id: Optional[str] = None
    attach_from_inbox_id: Optional[str] = None
    reply_to_inbox_id: Optional[str] = None


class EmailRecord(BaseModel):
    id: str
    account_id: str = ""
    message_id: str
    from_email: str
    to_email: str
    cc_email: str = ""
    # Raw decoded From / To / Cc headers (display name + address, comma-
    # joined for multi-recipient cases). These preserve the full recipient
    # list that the legacy `to_email` / `from_email` fields lose because
    # `parseaddr` only returns one address. Populated for emails fetched
    # after this feature shipped; older records leave these empty and the
    # frontend falls back to the singleton fields.
    from_raw: str = ""
    to_raw: str = ""
    cc_raw: str = ""
    subject: str
    body: str
    body_html: Optional[str] = None
    # List endpoint sets this to True/False without sending body_html itself,
    # so the client knows whether to offer the HTML tab (and trigger a
    # follow-up /body fetch). None on stored records — list serialization
    # derives it from body_html at read time.
    has_html: Optional[bool] = None
    received_at: str
    category: str
    attachments: List[Attachment] = Field(default_factory=list)
    read: bool = False
    replied: bool = False
    pinned: bool = False
    important: bool = False
    # "Handled" flag for important mail. When the user clicks 「已处理」 on
    # an important email, we keep `important=True` (the lesson lives on as
    # an experience) but flip this to True so the sidebar count drops and
    # the row badge shows the faded variant. Always False for non-important.
    handled: bool = False
    deleted: bool = False
    deleted_at: Optional[str] = None
    imap_uid: Optional[str] = None
    imap_mailbox: Optional[str] = None
    in_reply_to: Optional[str] = None
    references: List[str] = Field(default_factory=list)
    # Thread/source key used by the frontend "同源邮件合并" view.
    # Prefer the earliest References msg-id, then In-Reply-To, then self Message-Id.
    source_message_id: Optional[str] = None
    spam_reason: Optional[str] = None


class EmailUpdate(BaseModel):
    read: Optional[bool] = None
    replied: Optional[bool] = None
    pinned: Optional[bool] = None
    important: Optional[bool] = None
    handled: Optional[bool] = None
    category: Optional[str] = None
    deleted: Optional[bool] = None


class SyncSettings(BaseModel):
    sync_sent: bool = False
    sync_flags: bool = False
    sync_deletes: bool = False
    sync_folders: bool = False
    # How many days of mail to look back when receiving. Combined with the
    # per-mailbox UID watermark (see receive_emails) to give true incremental
    # fetches: every receive only pulls mail received within the last
    # `fetch_days` days that we haven't already seen.
    fetch_days: int = Field(default=30, ge=1, le=100)


class ConfigPayload(BaseModel):
    settings: EmailSettings
    sync: Optional[SyncSettings] = None


class Account(BaseModel):
    id: str
    label: str = ""
    settings: EmailSettings
    sync: SyncSettings = Field(default_factory=SyncSettings)
    owner_user_id: Optional[str] = None


class AccountCreate(BaseModel):
    label: str = ""
    settings: EmailSettings
    sync: Optional[SyncSettings] = None


class AccountUpdate(BaseModel):
    label: Optional[str] = None
    settings: Optional[EmailSettings] = None
    sync: Optional[SyncSettings] = None


class SendResult(BaseModel):
    status: Literal["ok"]
    detail: str


class ReceiveResult(BaseModel):
    status: Literal["ok"]
    fetched: int
    stored: int


class DraftPayload(BaseModel):
    id: Optional[str] = None
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    body: str = ""
    attach_from_inbox_id: Optional[str] = None


class DraftRecord(BaseModel):
    id: str
    account_id: str = ""
    to: str = ""
    cc: str = ""
    bcc: str = ""
    subject: str = ""
    body: str = ""
    updated_at: str
    attachments: List[Attachment] = Field(default_factory=list)


class SentRecord(BaseModel):
    id: str
    account_id: str = ""
    from_email: str
    to_email: str
    cc_email: str = ""
    bcc_email: str = ""
    subject: str = ""
    body: str = ""
    sent_at: str
    attachments: List[Attachment] = Field(default_factory=list)
    # Soft-delete: mirrors EmailRecord. When True the record sits in the
    # 回收站 instead of 已发送, and the user can either restore it or hard-
    # purge it. Older records (pre-this-feature) default to False.
    deleted: bool = False
    deleted_at: Optional[str] = None
    # Threading: when this sent record is a reply, we remember which inbox
    # email it answered (reply_to_inbox_id) and the source message-id of
    # that thread (source_message_id). Lets the inbox merge-view fold the
    # outgoing reply into the same row as the original. None for fresh
    # composes (not a reply).
    reply_to_inbox_id: Optional[str] = None
    source_message_id: Optional[str] = None
    in_reply_to: Optional[str] = None


class SentUpdate(BaseModel):
    """Minimal mutation payload for sent records. Today the only field a
    sent record exposes is the soft-delete flag — sent message bodies /
    headers are immutable. Modelled as Optional[bool] so the route can
    distinguish "explicit false" from "field omitted"."""

    deleted: Optional[bool] = None


# -------- users / auth --------

UserRole = Literal["admin", "normal"]


class User(BaseModel):
    id: str
    username: str
    role: UserRole
    active_account_id: Optional[str] = None
    created_at: str


class UserLogin(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=200)


class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=200)


class AdminUserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6, max_length=200)
    role: UserRole = "normal"


class PasswordReset(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=200)


class AuthStatus(BaseModel):
    initialized: bool
    current_user: Optional[User] = None


# -------- spam classification prompts --------

class UserPrompt(BaseModel):
    id: str
    account_id: str
    user_id: str
    username: str = ""  # decorated server-side for display, not stored
    name: str = ""      # human-friendly identifier, unique within account; used for @ref
    text: str
    target_folder: Optional[str] = None
    created_at: str
    updated_at: Optional[str] = None


class UserPromptCreate(BaseModel):
    name: str = Field(default="", max_length=48)
    text: str = Field(..., min_length=1, max_length=2000)
    target_folder: Optional[str] = None


class UserPromptUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=48)
    text: str = Field(..., min_length=1, max_length=2000)
    target_folder: Optional[str] = None


class SystemPromptUpdate(BaseModel):
    text: str = Field(..., max_length=4000)


class Experience(BaseModel):
    """A user-curated lesson the classifier should keep in mind. Surfaced
    to the LLM as additional general guidance, so it influences future
    classification decisions much like a target-less prompt. Most are
    auto-distilled from the「为何重要/为何取消重要」dialog, but the user
    can also edit / delete / write them manually."""

    id: str
    account_id: str
    user_id: str
    username: str = ""  # decorated server-side for display, not stored
    text: str
    source: str = ""  # e.g. "manual", "important-mark", "important-unmark"
    source_email_id: Optional[str] = None
    created_at: str
    updated_at: Optional[str] = None


class ExperienceCreate(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)
    source: str = Field(default="manual", max_length=32)
    source_email_id: Optional[str] = None


class ExperienceUpdate(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)


class ComposeDraftRequest(BaseModel):
    """Compose-window「自动生成」payload — same shape as a reply intent
    but no original email is referenced. The signature comes from the
    active account's settings.

    `language` lets the user pin the output language regardless of what
    language their intent text is in. Default is 中文 per the product spec.
    Reused by the forward panel (no original-email context required)."""

    intent: str = Field(..., min_length=1, max_length=2000)
    language: Literal["zh", "en"] = "zh"


class ComposeDraftResult(BaseModel):
    body_text: str


class ReplySummaryResult(BaseModel):
    """Reply-window pre-flight: a short Chinese summary of the original
    email (only produced for English mail; non-English returns an empty
    summary with `is_english=False`)."""

    is_english: bool
    summary: str = ""


class ReplyGenerationRequest(BaseModel):
    """Compose-window「自动回复」payload: the email being replied to plus
    a short user-supplied intent describing what the reply should say.

    `language` pins the output language; default 中文. Overrides the
    historical auto-detect-from-original behavior so users replying in
    Chinese to an English thread (or vice versa) get what they asked for."""

    intent: str = Field(..., min_length=1, max_length=2000)
    language: Literal["zh", "en"] = "zh"


class ReplyGenerationResult(BaseModel):
    reply_text: str


class ImportanceToggleRequest(BaseModel):
    """The「重要」toggle dialog payload: which email, which direction, and
    why. The server flips the flag, asks the LLM to distill a one-line
    experience from (email + reason), and persists that experience."""

    email_id: str
    mark_important: bool
    reason: str = Field(..., min_length=1, max_length=500)


class ImportanceToggleResult(BaseModel):
    email: Optional["EmailRecord"] = None
    experience: Optional[Experience] = None


class RecategorizeRequest(BaseModel):
    """The「移动到」对话框 payload: which email, the destination category,
    and why the user thinks it belongs there. The server moves the email,
    asks the LLM to distill a one-line experience from (email + reason +
    old/new category), and persists that experience."""

    email_id: str
    new_category: str = Field(..., min_length=1, max_length=200)
    reason: str = Field(..., min_length=1, max_length=500)


class RecategorizeResult(BaseModel):
    email: Optional["EmailRecord"] = None
    experience: Optional[Experience] = None


class LlmConfigStatus(BaseModel):
    """Public read view of the global LLM config. Never echoes the API key."""

    configured: bool
    model: str
    provider: str = "DeepSeek"


class SystemModeStatus(BaseModel):
    """Read view of the global runtime mode (dev / prod). Returned to every
    authenticated user so the main page can hide debug-only controls in prod."""

    mode: str


class SystemModeUpdate(BaseModel):
    """Admin-only write payload for switching dev <-> prod."""

    mode: str = Field(..., pattern=r"^(dev|prod)$")


class LlmConfigUpdate(BaseModel):
    """Settings-page payload for setting the DeepSeek API key. Empty / blank
    is rejected at the route layer."""

    api_key: str = Field(..., min_length=1, max_length=512)


class LlmFieldConfig(BaseModel):
    """Per-account toggle of which raw email fields to include in the
    user-message payload sent to Qwen during classification."""

    include_from: bool = True
    include_to: bool = False
    include_subject: bool = True
    include_body: bool = True
    include_attachments: bool = False
    body_char_cap: int = Field(default=2000, ge=0, le=10000)


class FixedRule(BaseModel):
    """A deterministic, non-LLM classification rule. The receive pipeline
    evaluates fixed rules BEFORE invoking DeepSeek; first match wins.

    `name` is a short identifier (unique within the account) that other
    rules / prompts can reference with @name. `nl_text` is what the user
    typed (may itself contain @refs); `program` is the AST DeepSeek
    produced after server-side expansion of those refs. Runtime
    classification only reads `program`.
    """

    id: str
    account_id: str
    user_id: str
    username: str = ""  # decorated server-side for display, not stored
    name: str = ""      # account-unique identifier; targetable by @name
    nl_text: str
    explanation: str = ""
    program: Dict = Field(default_factory=dict)
    code_preview: str = ""
    refs: List[str] = Field(default_factory=list)  # denormalized @name dependencies
    target_folder: str
    created_at: str
    updated_at: Optional[str] = None


class FixedRuleCompileRequest(BaseModel):
    """Settings panel: 'please translate this NL into a program for me to
    review before I save it.' No persistence at this stage. `name` and
    `editing_id` are passed so the server can do cycle detection against the
    rule that's about to be created/updated."""

    nl_text: str = Field(..., min_length=1, max_length=2000)
    target_folder: str = Field(..., min_length=1, max_length=128)
    name: str = Field(default="", max_length=48)
    editing_id: Optional[str] = None


class FixedRuleCompileResponse(BaseModel):
    nl_text: str
    target_folder: str
    explanation: str
    code_preview: str
    program: Dict
    name: str = ""
    expanded_nl: str = ""           # NL after @ref substitution, for transparency
    refs: List[str] = Field(default_factory=list)


class FixedRuleValidateRequest(BaseModel):
    """Settings panel: 'I edited the AST by hand — is it still well-formed?'
    Returns errors and a refreshed pseudo-code preview, but does not persist."""

    program: Dict


class FixedRuleValidateResponse(BaseModel):
    valid: bool
    errors: List[str] = Field(default_factory=list)
    code_preview: str = ""


class FixedRuleCreate(BaseModel):
    """Save the user-confirmed translation. The client should send back
    exactly what /compile returned (plus the chosen folder) so the
    explanation and pseudo-code shown to the user are the ones we persist."""

    name: str = Field(default="", max_length=48)
    nl_text: str = Field(..., min_length=1, max_length=2000)
    explanation: str = Field(default="", max_length=500)
    code_preview: str = Field(default="", max_length=4000)
    program: Dict
    refs: List[str] = Field(default_factory=list)
    target_folder: str = Field(..., min_length=1, max_length=128)


class FixedRuleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=48)
    nl_text: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    explanation: Optional[str] = Field(default=None, max_length=500)
    code_preview: Optional[str] = Field(default=None, max_length=4000)
    program: Optional[Dict] = None
    refs: Optional[List[str]] = None
    target_folder: Optional[str] = Field(default=None, min_length=1, max_length=128)


class FixedRuleReorder(BaseModel):
    """Replace the rule list for the active account with this exact ordering;
    earlier IDs win when multiple rules could match an email."""

    rule_ids: List[str] = Field(..., min_length=0)


class PromptsView(BaseModel):
    system: str
    system_is_default: bool
    items: List[UserPrompt] = Field(default_factory=list)
    fixed_rules: List[FixedRule] = Field(default_factory=list)
    experiences: List[Experience] = Field(default_factory=list)
    field_config: LlmFieldConfig = Field(default_factory=LlmFieldConfig)
    available_folders: List[str] = Field(default_factory=list)


class ClassifyUnsortedResult(BaseModel):
    classified: int
    remaining: int
    total: int


# -------- contacts (per-account address book) --------

class Contact(BaseModel):
    id: str
    account_id: str = ""
    name: str = ""
    email: EmailStr
    tags: List[str] = Field(default_factory=list)
    note: str = ""
    created_at: str
    updated_at: Optional[str] = None


class ContactCreate(BaseModel):
    name: str = Field(default="", max_length=120)
    email: EmailStr
    tags: List[str] = Field(default_factory=list)
    note: str = Field(default="", max_length=2000)


class ContactUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    email: Optional[EmailStr] = None
    tags: Optional[List[str]] = None
    note: Optional[str] = Field(default=None, max_length=2000)
