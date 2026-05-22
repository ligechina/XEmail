# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""LLM-backed multi-folder email classifier.

Calls DeepSeek's OpenAI-compatible chat-completions endpoint with the
`deepseek-chat` model (DeepSeek V4). Designed to be fail-open: if the API
key is missing, the call errors, or the response is unparseable, we return
an empty category — the caller is then free to fall back (e.g. tag the
email as 未分类).
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
_MODEL = "deepseek-chat"
_TIMEOUT_SEC = 15
_DEFAULT_BODY_CHAR_CAP = 2000


def _resolve_api_key() -> str:
    """Storage-configured key first; fall back to env var. Storage wins so the
    Settings page is the source of truth."""
    try:
        from app.storage import read_llm_api_key

        stored = (read_llm_api_key() or "").strip()
        if stored:
            return stored
    except Exception:
        pass
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def is_api_key_configured() -> bool:
    return bool(_resolve_api_key())

_DEFAULT_FIELD_CONFIG: Dict = {
    "include_from": True,
    "include_to": False,
    "include_subject": True,
    "include_body": True,
    "include_attachments": False,
    "body_char_cap": _DEFAULT_BODY_CHAR_CAP,
}

# Built-in system prompt for multi-folder classification. Admin can override
# via the panel; we always append a strict output-format footer so old/new
# overrides can't break the JSON contract.
DEFAULT_SYSTEM_PROMPT = (
    "你是一名邮件分类助手。请把每封邮件归入一个最合适的文件夹，"
    "同时判断这封邮件是否需要被标记为「重要」（important）。\n"
    "默认规则：spam / 钓鱼 / 营销轰炸 / 诈骗 / 勒索 归入「垃圾邮件」；"
    "正常工作通知、账单、社交邀请、订阅确认、验证码邮件不是垃圾邮件。\n"
    "重要性默认 false。当用户在下方提示中指定了"
    "「应被标记为重要」的情形时，按用户指引设置 important=true；"
    "否则保持 false。垃圾邮件永远不应被标记为重要。\n"
    "如果实在无法判断分类，请返回空字符串作为 category，由系统记为「未分类」。"
)

_OUTPUT_FOOTER = (
    "只返回 JSON，严格格式为："
    "{\"category\": \"<文件夹名 或 空字符串>\","
    " \"important\": <true 或 false>,"
    " \"reason\": \"<不超过40字的中文理由>\"}。"
    "不要输出 JSON 以外的任何字符。"
)


def _compose_system_prompt(
    system_prompt: Optional[str],
    user_prompts_with_targets: Optional[List[Dict[str, Optional[str]]]],
    available_folders: Optional[List[str]],
) -> str:
    """Stitch the configurable base prompt together with the dynamic context.

    Format:
        <admin or default base>
        可选文件夹: A / B / C
        分组的用户规则（带 target_folder vs 通用指引）
        严格输出格式（始终追加）
    """
    base = (system_prompt or DEFAULT_SYSTEM_PROMPT).strip() or DEFAULT_SYSTEM_PROMPT

    parts: List[str] = [base]
    if available_folders:
        parts.append("可选文件夹（必须从中选一个，或返回空字符串）：" + " / ".join(available_folders))

    rules_with_target: List[str] = []
    general_rules: List[str] = []
    for item in user_prompts_with_targets or []:
        if not item:
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        target = (item.get("target_folder") or "").strip()
        # Sentinel "*" means "全部 / applies to all emails" — treat as general
        # guidance rather than a routing rule with a concrete destination.
        if target and target != "*":
            rules_with_target.append(f"→ [{target}] {text}")
        else:
            general_rules.append(text)

    if rules_with_target:
        numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules_with_target))
        parts.append(
            "用户分类规则（任一命中则输出对应文件夹名）：\n" + numbered
        )
    if general_rules:
        numbered = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(general_rules))
        parts.append(
            "其他通用指引（参考，不直接对应单一文件夹）：\n" + numbered
        )

    parts.append(_OUTPUT_FOOTER)
    return "\n\n".join(parts)


def _build_user_content(
    from_email: str,
    to_email: str,
    subject: str,
    body: str,
    attachments: Optional[List[str]],
    cfg: Dict,
) -> str:
    """Assemble the user-message payload according to the field config."""
    parts: List[str] = []
    if cfg.get("include_from", True):
        parts.append(f"发件人: {from_email or '(unknown)'}")
    if cfg.get("include_to", False):
        parts.append(f"收件人: {to_email or '(unknown)'}")
    if cfg.get("include_subject", True):
        parts.append(f"主题: {subject or '(empty)'}")
    if cfg.get("include_attachments", False):
        names = [n for n in (attachments or []) if n]
        joined = "、".join(names) if names else "(无)"
        parts.append(f"附件文件名: {joined}")
    if cfg.get("include_body", True):
        cap = int(cfg.get("body_char_cap", _DEFAULT_BODY_CHAR_CAP) or 0)
        truncated = (body or "")[:cap] if cap > 0 else ""
        parts.append(f"正文:\n{truncated}")
    return "\n".join(parts) if parts else "(没有可用字段)"


def classify_via_llm(
    from_email: str,
    subject: str,
    body: str,
    *,
    to_email: str = "",
    attachments: Optional[List[str]] = None,
    system_prompt: Optional[str] = None,
    user_prompts_with_targets: Optional[List[Dict[str, Optional[str]]]] = None,
    available_folders: Optional[List[str]] = None,
    field_config: Optional[Dict] = None,
) -> Tuple[str, bool, str]:
    """Return (category, important, reason).

    `category` is one of `available_folders` or "" (no opinion / API
    unavailable). The receive pipeline maps "" → 未分类.

    `important` is the LLM's verdict on whether the email should be flagged
    as 重要. It only carries authority when the LLM actually answered;
    transport / no-key errors return False so the existing flag is left
    untouched by callers (see classify_email_record).
    """
    api_key = _resolve_api_key()
    if not api_key:
        return "", False, "no api key"

    cfg = {**_DEFAULT_FIELD_CONFIG, **(field_config or {})}
    user_content = _build_user_content(
        from_email, to_email, subject, body, attachments, cfg
    )

    composed_system = _compose_system_prompt(
        system_prompt, user_prompts_with_targets, available_folders
    )

    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": composed_system},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
        "max_tokens": 160,
        "response_format": {"type": "json_object"},
    }

    last_err = ""
    for attempt in range(2):
        try:
            cat, important, reason = _call(api_key, payload)
            return (
                _normalize_category(cat, available_folders),
                important,
                reason,
            )
        except Exception as exc:
            last_err = str(exc) or exc.__class__.__name__
            logger.warning(
                "classifier call failed (attempt %d): %s", attempt + 1, last_err
            )
            if attempt == 0:
                time.sleep(1.0)

    return "", False, f"llm error: {last_err}"


def _normalize_category(
    raw: str, available_folders: Optional[List[str]]
) -> str:
    """Trim the model output to a known folder; otherwise drop it."""
    candidate = (raw or "").strip()
    if not candidate:
        return ""
    if not available_folders:
        return candidate  # caller will validate
    # Exact match first, then case-insensitive fallback (model occasionally
    # uppercases or swaps full/half-width characters).
    if candidate in available_folders:
        return candidate
    lower = candidate.lower()
    for f in available_folders:
        if f.lower() == lower:
            return f
    return ""


def distill_experience(
    *,
    direction: str,
    user_reason: str,
    from_email: str = "",
    to_email: str = "",
    subject: str = "",
    body: str = "",
    body_char_cap: int = 800,
) -> str:
    """Ask the LLM to compress a user's "this email is/isn't important
    because…" explanation into a single-sentence rule the classifier can
    apply to *future* mail. Returns the distilled text (no JSON wrapper).

    `direction` is "mark" or "unmark". On any error (no API key, transport
    failure, garbage output) returns "" — caller should fall back to using
    the user's raw reason verbatim.
    """
    direction_label = "标为「重要」" if direction == "mark" else "取消「重要」标记"
    body_excerpt = (body or "")[:max(0, body_char_cap)]
    user_msg = (
        f"用户对一封邮件做了 {direction_label} 的操作，并给出了原因。"
        "请把这条原因提炼成一条**通用、可复用**的判断经验，写成一句完整的中文，"
        "不超过 80 字，便于以后系统对类似邮件做相同的判断。\n"
        "只返回这一句话，不要任何前后缀、不要 JSON、不要列表序号。\n\n"
        f"---\n用户原因: {user_reason.strip()}\n\n"
        f"邮件主题: {subject or '(无)'}\n"
        f"发件人: {from_email or '(unknown)'}\n"
        f"收件人: {to_email or '(unknown)'}\n"
        f"正文摘录:\n{body_excerpt or '(无)'}"
    )
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你帮助用户把一次性的判断理由提炼成可被分类系统反复套用的"
                    "经验。语言简洁、聚焦邮件特征（发件人、主题、正文模式等），"
                    "避免提及具体邮件标题。"
                ),
            },
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
    }
    try:
        text = chat_completion(payload).strip()
    except Exception as exc:
        logger.warning("distill_experience failed: %s", exc)
        return ""
    # Strip surrounding quotes / leading bullets the model sometimes adds.
    text = text.strip().strip("「」\"'").strip()
    if text.startswith(("- ", "• ", "* ")):
        text = text[2:].strip()
    return text[:240]


def distill_category_experience(
    *,
    from_category: str,
    to_category: str,
    user_reason: str,
    from_email: str = "",
    to_email: str = "",
    subject: str = "",
    body: str = "",
    body_char_cap: int = 800,
) -> str:
    """Compress a "this email belongs in X because…" explanation into a
    one-sentence rule the classifier can apply to future mail. The output
    deliberately mentions the destination category so the next time a
    similar email arrives it lands there directly.

    Returns "" on any error (no API key, transport failure, garbage output)
    — caller should fall back to a verbatim phrasing of the user's reason.
    """
    body_excerpt = (body or "")[:max(0, body_char_cap)]
    from_label = (from_category or "").strip() or "未分类"
    to_label = (to_category or "").strip() or "(未指定)"
    user_msg = (
        f"用户把一封邮件从分类「{from_label}」改到分类「{to_label}」，并给出了原因。"
        "请把这条原因提炼成一条**通用、可复用**的判断经验，"
        f"明确说明什么样的邮件应被归入「{to_label}」，"
        "写成一句完整的中文，不超过 80 字，便于以后系统对类似邮件直接做出正确分类。\n"
        "只返回这一句话，不要任何前后缀、不要 JSON、不要列表序号、不要重复用户原话。\n\n"
        f"---\n用户原因: {user_reason.strip()}\n\n"
        f"邮件主题: {subject or '(无)'}\n"
        f"发件人: {from_email or '(unknown)'}\n"
        f"收件人: {to_email or '(unknown)'}\n"
        f"正文摘录:\n{body_excerpt or '(无)'}"
    )
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你帮助用户把一次性的分类调整理由提炼成可被分类系统反复套用的"
                    "经验。语言简洁、聚焦邮件特征（发件人、主题、正文模式等），"
                    "并明确给出目标分类。避免提及具体邮件标题。"
                ),
            },
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
    }
    try:
        text = chat_completion(payload).strip()
    except Exception as exc:
        logger.warning("distill_category_experience failed: %s", exc)
        return ""
    text = text.strip().strip("「」\"'").strip()
    if text.startswith(("- ", "• ", "* ")):
        text = text[2:].strip()
    return text[:240]


def generate_reply(
    *,
    original_from: str,
    original_to: str,
    original_subject: str,
    original_body: str,
    intent: str,
    signature: str = "",
    body_char_cap: int = 2500,
    language: str = "zh",
) -> str:
    """Compose a polite, ready-to-send reply using DeepSeek.

    The LLM is told to:
      • detect the original email's primary language and reply in it
        (English in → English out; Chinese in → Chinese out);
      • follow the user's intent (`intent`) for the reply's content;
      • append the user's signature **verbatim** at the very end (or omit
        it entirely if `signature` is empty);
      • return ONLY the body of the reply — no subject line, no
        quoted-original block, no JSON wrapper.

    Returns the generated text. Raises RuntimeError on transport failure
    so the caller can surface a clean error to the UI.
    """
    body_excerpt = (original_body or "")[:max(0, body_char_cap)]
    sig_section = (signature or "").strip()
    sig_instruction = (
        f"落款（必须原样追加在回复末尾，与正文之间空一行）：\n{sig_section}"
        if sig_section
        else "落款：无（不要自行编造任何落款署名）。"
    )
    user_msg = (
        "原邮件:\n"
        f"  发件人: {original_from or '(unknown)'}\n"
        f"  收件人: {original_to or '(unknown)'}\n"
        f"  主题: {original_subject or '(无)'}\n"
        f"  正文:\n{body_excerpt or '(无)'}\n\n"
        f"用户的回复意图:\n{intent.strip()}\n\n"
        f"{sig_instruction}"
    )
    # Hard pin the output language. The user picks 中文 / 英文 in the UI
    # (defaults to 中文); we forbid auto-detect because it surprises users
    # who, e.g., reply in Chinese to an English thread.
    if (language or "zh").lower() == "en":
        language_rule = (
            "语言规则：请用英文撰写整封回复，包含英文称呼（Dear X / Hi X）"
            "与英文结尾（Best regards / Thanks）。即使用户的回复意图或"
            "原邮件是中文也必须用英文输出。"
        )
    else:
        language_rule = (
            "语言规则：请用中文撰写整封回复，包含中文称呼（您好 / X 老师）"
            "与中文结尾（此致 / 顺颂时祺 / 祝好 等）。即使用户的回复意图"
            "或原邮件是英文也必须用中文输出。"
        )
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是邮件回复助手。根据「用户回复意图」和「原邮件」撰写一封"
                    "礼貌、简洁、自然的邮件回复。注意，当用户输入的回复意图是一个问题时，"
                    "用户的目的是希望你把这个问题修改为一封邮件正文，而不是让你回答这个问题。\n"
                    f"{language_rule}\n"
                    "结构规则：包含合适的称呼、回应意图、礼貌结尾。"
                    "不要重复原邮件正文，不要写主题，不要写"
                    "「-------- 原邮件 --------」之类的引文块。\n"
                    "落款规则：如果系统给出了落款，请原样附在最后并与正文空一行；"
                    "如果没有给出落款，不要自行编造署名。\n"
                    "只返回回复的正文文本，不要任何 JSON、Markdown 代码块或前后说明。"
                ),
            },
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.4,
        "max_tokens": 800,
    }
    try:
        text = chat_completion(payload).strip()
    except Exception as exc:
        logger.warning("generate_reply failed: %s", exc)
        raise RuntimeError(f"调用 DeepSeek 失败: {exc}") from exc
    # Strip surrounding code fences in case the model wrapped its answer.
    if text.startswith("```"):
        # Remove first line (``` or ```lang) and trailing ```
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def is_predominantly_english(text: str, *, threshold: float = 0.6) -> bool:
    """Cheap heuristic: returns True when ≥`threshold` of the *letter*
    characters in `text` are ASCII A–Z. We deliberately ignore digits,
    punctuation, whitespace, URLs and CJK punctuation — only the letter
    mix matters. An empty/letterless string returns False (nothing to
    summarize)."""
    if not text:
        return False
    ascii_letters = 0
    other_letters = 0
    for ch in text:
        if ch.isalpha():
            if ord(ch) < 128:
                ascii_letters += 1
            else:
                other_letters += 1
    total = ascii_letters + other_letters
    if total == 0:
        return False
    return (ascii_letters / total) >= threshold


def summarize_email_for_reply(
    *,
    from_email: str,
    subject: str,
    body: str,
    body_char_cap: int = 3000,
) -> str:
    """Produce a short Chinese summary of an English email aimed at
    helping the user draft a reply. Two sections:
        1) 邮件大意 — what the email is about
        2) 对方诉求 — what the sender is asking for / expects
    Returns plain text (no JSON wrapper). Raises RuntimeError on
    transport failure so the caller can surface a clean error."""
    body_excerpt = (body or "")[:max(0, body_char_cap)]
    user_msg = (
        "请用中文总结下面这封英文邮件，帮助我决定怎么回复。\n"
        "格式严格如下，两段，每段一行（≤80 字）：\n"
        "邮件大意：<一句话概括邮件主题与背景>\n"
        "对方诉求：<一句话说明对方希望我做什么 / 期待什么回复>\n\n"
        f"---\n发件人: {from_email or '(unknown)'}\n"
        f"主题: {subject or '(无)'}\n\n"
        f"正文:\n{body_excerpt or '(无)'}"
    )
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是一名邮件助理，把英文来信压缩成两行中文摘要，"
                    "聚焦事实与请求，不加评论、不加客套。"
                    "只输出指定格式的两行内容，不要任何前后缀或 Markdown。"
                ),
            },
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
        "max_tokens": 280,
    }
    try:
        text = chat_completion(payload).strip()
    except Exception as exc:
        logger.warning("summarize_email_for_reply failed: %s", exc)
        raise RuntimeError(f"调用 DeepSeek 失败: {exc}") from exc
    # Drop accidental code fences.
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def generate_compose_draft(
    *,
    intent: str,
    signature: str = "",
    language: str = "zh",
) -> str:
    """Draft a brand-new email body from the user's intent (no original
    email to react to). Mirrors `generate_reply` but with no quoted
    context: the LLM picks the language from the intent itself, writes a
    polite full email body, and appends the user's signature verbatim.

    Returns the body text. Raises RuntimeError on transport failure so
    the caller can surface a clean error to the UI.
    """
    sig_section = (signature or "").strip()
    sig_instruction = (
        f"落款（必须原样追加在末尾，与正文之间空一行）：\n{sig_section}"
        if sig_section
        else "落款：无（不要自行编造任何落款署名）。"
    )
    user_msg = (
        f"用户的撰写意图:\n{intent.strip()}\n\n"
        f"{sig_instruction}"
    )
    # Same rationale as generate_reply: respect the user's explicit
    # 中文 / 英文 choice; never auto-detect from the intent text.
    if (language or "zh").lower() == "en":
        language_rule = (
            "语言规则：请用英文撰写整封邮件，包含英文称呼（Dear X / Hi X）"
            "与英文结尾（Best regards / Thanks）。即使用户的撰写意图为"
            "中文也必须用英文输出。"
        )
    else:
        language_rule = (
            "语言规则：请用中文撰写整封邮件，包含中文称呼（您好 / X 老师）"
            "与中文结尾。即使用户的撰写意图为英文也必须用中文输出。"
        )
    payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是邮件撰写助手。根据用户的撰写意图起草一封礼貌、"
                    "简洁、自然的邮件正文。注意，当用户输入的撰写意图是一个问题时，"
                    "用户的目的是希望你把这个问题修改为一封邮件正文，而不是让你回答这个问题。\n"
                    f"{language_rule}\n"
                    "结构规则：包含合适的称呼、表达内容、礼貌结尾。"
                    "不要写主题，不要写「-------- 原邮件 --------」"
                    "之类的引文块，不要写任何说明性的元信息。\n"
                    "落款规则：如果系统给出了落款，请原样附在最后并与正文空一行；"
                    "如果没有给出落款，不要自行编造署名。\n"
                    "只返回邮件正文本身，不要任何 JSON、Markdown 代码块或前后说明。"
                ),
            },
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.4,
        "max_tokens": 800,
    }
    try:
        text = chat_completion(payload).strip()
    except Exception as exc:
        logger.warning("generate_compose_draft failed: %s", exc)
        raise RuntimeError(f"调用 DeepSeek 失败: {exc}") from exc
    # Strip surrounding code fences just in case.
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def chat_completion(payload: dict, *, timeout: Optional[int] = None) -> str:
    """Generic DeepSeek chat-completions call. Resolves the API key from
    storage/env, posts the payload, and returns the assistant message
    content (raw string). Caller is responsible for parsing it (JSON or
    otherwise). Raises on missing key, HTTP error, or empty content."""
    api_key = _resolve_api_key()
    if not api_key:
        raise RuntimeError("no api key")
    payload = {"model": _MODEL, **payload}
    req = urllib.request.Request(
        _ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout or _TIMEOUT_SEC) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    content = (
        data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    ).strip()
    if not content:
        raise ValueError("empty content in response")
    return content


def _call(api_key: str, payload: dict) -> Tuple[str, bool, str]:
    # api_key arg is ignored; chat_completion resolves it via storage/env.
    # Kept to preserve the legacy signature inside classify_via_llm.
    content = chat_completion(payload)
    parsed = _parse_response_content(content)
    category = str(parsed.get("category", "") or "").strip()
    reason = str(parsed.get("reason", "") or "")
    important_raw = parsed.get("important")
    if isinstance(important_raw, bool):
        important = important_raw
    elif isinstance(important_raw, str):
        # Tolerate "true" / "false" / "1" / "0" — DeepSeek occasionally
        # stringifies booleans when the system prompt was hand-edited.
        important = important_raw.strip().lower() in ("true", "1", "yes", "y")
    else:
        important = False
    # Back-compat: old system prompts asked for {is_spam, reason}. Map true
    # to "垃圾邮件" so admin overrides written for the old contract still work.
    if not category and isinstance(parsed.get("is_spam"), bool):
        category = "垃圾邮件" if parsed["is_spam"] else ""
    return category, important, reason


def _parse_response_content(content: str) -> Dict[str, Any]:
    try:
        return json.loads(content)
    except Exception:
        return {}
