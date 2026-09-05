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

Calls DeepSeek's OpenAI-compatible chat-completions endpoint. Designed to
be fail-open: if the API key is missing, the call errors, or the response
is unparseable, we return an empty category — the caller is then free to
fall back (e.g. tag the email as 未分类).
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Direct HTTP opener that ignores the system's HTTP/HTTPS proxy config.
# Used as a fallback when the default `urllib.request.urlopen` (which
# honors `getproxies()`) fails because the user has a debug proxy
# installed system-wide (MacPacket, Charles, Proxyman, Fiddler…) that
# either returns a `Tunnel connection failed: 5xx` for CONNECT requests
# or refuses arbitrary remote endpoints. See _call_deepseek below.
_DIRECT_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
# `deepseek-chat` was DeepSeek's default until they retired it in favor of
# the v4 lineup. The old name now returns HTTP 400 with
# "supported API model names are deepseek-v4-pro or deepseek-v4-flash".
# `flash` is the drop-in equivalent — much cheaper per request and plenty
# smart for the short classify / summarize / reply-draft tasks here; use
# `deepseek-v4-pro` if you want max quality for reply drafts.
_MODEL = "deepseek-v4-flash"
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
    "include_to": True,
    "include_cc": True,
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
    *,
    cc_email: str = "",
    owner_email: str = "",
) -> str:
    """Assemble the user-message payload according to the field config.

    `owner_email`, when supplied, is placed at the top so prompts /
    experiences that reason about "我" (the current user) — e.g.
    "发给我 vs 抄送我" — actually have a reference point. Without it
    the LLM has no way to tell whether a To/Cc address is the user's
    own or somebody else's.
    """
    parts: List[str] = []
    if owner_email:
        parts.append(f"（当前账号邮箱: {owner_email}）")
    if cfg.get("include_from", True):
        parts.append(f"发件人: {from_email or '(unknown)'}")
    if cfg.get("include_to", True):
        parts.append(f"收件人(To): {to_email or '(unknown)'}")
    if cfg.get("include_cc", True):
        parts.append(f"抄送(Cc): {cc_email or '(无)'}")
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
    cc_email: str = "",
    owner_email: str = "",
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
        from_email, to_email, subject, body, attachments, cfg,
        cc_email=cc_email, owner_email=owner_email,
    )

    composed_system = _compose_system_prompt(
        system_prompt, user_prompts_with_targets, available_folders
    )

    # 4000 tokens is way more than the ~80-token JSON payload needs, but
    # DeepSeek v4-flash is a reasoning model — hidden `reasoning_tokens`
    # eat from the same max_tokens allowance before any visible content
    # comes out. The old 160-token budget was being 100% consumed by
    # reasoning on any non-trivial email, causing chat_completion to raise
    # `empty content in response`. That single misconfiguration was
    # responsible for ~90% of 未分类 mail in observed real corpora. The
    # per-token cost of unused headroom is zero; the cost of an under-
    # budget reasoning burn is a whole email misfiled.
    payload = {
        "model": _MODEL,
        "messages": [
            {"role": "system", "content": composed_system},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }

    last_err = ""
    # 3 attempts: two at 4k, one at 8k (doubles the reasoning budget in
    # case a particularly gnarly email needs more thinking). Each retry
    # sleeps 1s between attempts.
    for attempt in range(3):
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
            # Escalate budget after the first empty-content failure so a
            # single re-run is likely to succeed rather than falling all
            # the way through to 未分类.
            if "empty content" in last_err and payload["max_tokens"] < 8000:
                payload = {**payload, "max_tokens": 8000}
            if attempt < 2:
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


def suggest_recategorize_experience(
    *,
    from_category: str,
    to_category: str,
    from_email: str = "",
    to_email: str = "",
    subject: str = "",
    body: str = "",
    existing_experiences: Optional[List[Dict[str, str]]] = None,
    body_char_cap: int = 800,
) -> Dict:
    """A single LLM call that does two things at once:
      1. Generate a one-sentence classification-experience candidate from
         the user's implicit action (moved email from category X to Y).
      2. Compare against the account's existing experiences and decide
         whether the new candidate would be a semantic duplicate — if so,
         which existing id best covers it.

    Returns:
        {
          "candidate_text": "...",         # empty on total LLM failure
          "duplicate_of": "x_..." | None,  # id of existing experience
                                            # that already covers this case
          "reason": "..."                  # short explanation (why dup or
                                            # what pattern the candidate captures)
        }

    Never raises; on any error returns {"candidate_text": "", "duplicate_of": None,
    "reason": "..."} so the caller can fall back to a no-op or plain move.
    """
    from_label = (from_category or "").strip() or "未分类"
    to_label = (to_category or "").strip() or "(未指定)"
    body_excerpt = (body or "")[:max(0, body_char_cap)]

    existing = existing_experiences or []
    if existing:
        existing_block = "\n".join(
            f"[{e.get('id')}] {(e.get('text') or '').strip()}" for e in existing
        )
    else:
        existing_block = "(none)"

    system_msg = (
        "你是「分类经验生成 + 去重」助手。根据用户刚刚对一封邮件做的分类调整,"
        "生成一条通用的分类经验(≤80 字中文),同时检查这条经验是否与用户账号中"
        "已有的某条经验语义重复;若重复,返回该已有经验的 id 建议用户复用,不必新增。"
    )
    user_msg = (
        f"用户把一封邮件从「{from_label}」移动到「{to_label}」。\n\n"
        f"邮件主题: {subject or '(无)'}\n"
        f"发件人: {from_email or '(unknown)'}\n"
        f"收件人: {to_email or '(unknown)'}\n"
        f"正文摘录:\n{body_excerpt or '(无)'}\n\n"
        f"账号现有经验列表(格式:[id] 文本):\n{existing_block}\n\n"
        "请输出**严格 JSON**,顶级只包含以下字段:\n"
        '{\n'
        '  "candidate_text": "<一句中文经验,≤80 字,聚焦邮件特征 + 目标分类>",\n'
        '  "duplicate_of":   "<现有经验 id> 或 null>",\n'
        '  "reason":         "<20 字内说明:为何判定重复,或候选捕获的是什么特征>"\n'
        '}\n\n'
        "判断重复的标准:如果现有经验已经能覆盖这次调整背后的规则(即用户下次遇到"
        "同类邮件时,原有经验会让系统做出正确分类),则填 duplicate_of;否则填 null。\n"
        "❗ candidate_text 一律要生成,即使 duplicate_of 非 null 也要写(供用户参考)。\n"
        "❗ 不要输出 JSON 以外的任何字符,不要 Markdown 代码块。"
    )
    # Reasoning-model budget: 6k → escalate to 12k on empty-content (v4-flash
    # burns a variable amount of thinking budget depending on the existing-
    # experiences list length; a 30-item list can eat past 6k).
    def _run(max_tokens: int, timeout: int) -> str:
        payload = {
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        return chat_completion(payload, timeout=timeout).strip()

    try:
        raw = _run(max_tokens=6000, timeout=90)
    except ValueError as exc:
        if "empty content" not in str(exc):
            logger.warning("suggest_recategorize_experience call failed: %s", exc)
            return {"candidate_text": "", "duplicate_of": None,
                    "reason": f"LLM 失败:{exc}"}
        logger.warning("suggest_recategorize_experience: empty at 6k, retry at 12k")
        try:
            raw = _run(max_tokens=12000, timeout=180)
        except Exception as exc2:
            logger.warning("suggest_recategorize_experience retry failed: %s", exc2)
            return {"candidate_text": "", "duplicate_of": None,
                    "reason": f"LLM 失败(重试后仍无输出):{exc2}"}
    except Exception as exc:
        logger.warning("suggest_recategorize_experience call failed: %s", exc)
        return {"candidate_text": "", "duplicate_of": None,
                "reason": f"LLM 失败:{exc}"}
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("suggest_recategorize_experience JSON parse failed: %s | raw=%s",
                       exc, raw[:400])
        return {"candidate_text": "", "duplicate_of": None,
                "reason": "LLM 返回不是合法 JSON"}
    if not isinstance(parsed, dict):
        return {"candidate_text": "", "duplicate_of": None,
                "reason": "LLM 返回结构非对象"}
    # Validate + defense: duplicate_of must be a real id from the input
    # list; the LLM may hallucinate. If it does, drop it silently — the
    # experience will be added as new.
    dup = parsed.get("duplicate_of")
    if isinstance(dup, str):
        dup = dup.strip() or None
        if dup and existing and not any(e.get("id") == dup for e in existing):
            dup = None
    else:
        dup = None
    text = (parsed.get("candidate_text") or "").strip().strip("「」\"'").strip()
    if text.startswith(("- ", "• ", "* ")):
        text = text[2:].strip()
    return {
        "candidate_text": text[:240],
        "duplicate_of": dup,
        "reason": (parsed.get("reason") or "").strip()[:200],
    }


def suggest_importance_experience(
    *,
    direction: str,  # "mark" | "unmark"
    from_email: str = "",
    to_email: str = "",
    subject: str = "",
    body: str = "",
    existing_experiences: Optional[List[Dict[str, str]]] = None,
    body_char_cap: int = 800,
) -> Dict:
    """Same shape as suggest_recategorize_experience, but for the ⭐ ← flip.
    The user just marked (or unmarked) an email as important; generate a
    one-sentence rule that would let the classifier make the same call
    on future mail, and check for semantic overlap with existing
    experiences. Returns the same {candidate_text, duplicate_of, reason}
    dict — never raises."""
    action_label = "标为「重要」" if direction == "mark" else "取消「重要」标记"
    goal_hint = (
        "识别哪类邮件应当被标为重要"
        if direction == "mark"
        else "识别哪类邮件不应该被标为重要"
    )
    body_excerpt = (body or "")[:max(0, body_char_cap)]

    existing = existing_experiences or []
    if existing:
        existing_block = "\n".join(
            f"[{e.get('id')}] {(e.get('text') or '').strip()}" for e in existing
        )
    else:
        existing_block = "(none)"

    system_msg = (
        "你是「重要邮件经验生成 + 去重」助手。用户刚刚对一封邮件做了 ⭐ 重要标签"
        "的调整;根据邮件本身的特征,推断出用户的判断逻辑,生成一条通用的判断"
        "经验(≤80 字中文),用来指导系统对未来同类邮件做出相同判断。同时检查这条"
        "经验是否与用户账号中已有的某条经验语义重复;若重复,返回该已有经验的 id "
        "建议用户复用。"
    )
    user_msg = (
        f"用户对一封邮件执行了 {action_label} 的操作(目标:{goal_hint})。\n\n"
        f"邮件主题: {subject or '(无)'}\n"
        f"发件人: {from_email or '(unknown)'}\n"
        f"收件人: {to_email or '(unknown)'}\n"
        f"正文摘录:\n{body_excerpt or '(无)'}\n\n"
        f"账号现有经验列表(格式:[id] 文本):\n{existing_block}\n\n"
        "请输出**严格 JSON**,顶级只包含以下字段:\n"
        '{\n'
        '  "candidate_text": "<一句中文经验,≤80 字,聚焦邮件特征 + 重要 or 不重要>",\n'
        '  "duplicate_of":   "<现有经验 id> 或 null>",\n'
        '  "reason":         "<20 字内说明:为何判定重复,或候选捕获的是什么特征>"\n'
        '}\n\n'
        "判断重复的标准:如果现有经验已经能覆盖这次调整背后的规则,则填 duplicate_of;否则填 null。\n"
        "❗ candidate_text 一律要生成,即使 duplicate_of 非 null 也要写(供用户参考)。\n"
        "❗ 不要输出 JSON 以外的任何字符,不要 Markdown 代码块。"
    )

    def _run(max_tokens: int, timeout: int) -> str:
        payload = {
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        return chat_completion(payload, timeout=timeout).strip()

    try:
        raw = _run(max_tokens=6000, timeout=90)
    except ValueError as exc:
        if "empty content" not in str(exc):
            logger.warning("suggest_importance_experience call failed: %s", exc)
            return {"candidate_text": "", "duplicate_of": None,
                    "reason": f"LLM 失败:{exc}"}
        logger.warning("suggest_importance_experience: empty at 6k, retry at 12k")
        try:
            raw = _run(max_tokens=12000, timeout=180)
        except Exception as exc2:
            logger.warning("suggest_importance_experience retry failed: %s", exc2)
            return {"candidate_text": "", "duplicate_of": None,
                    "reason": f"LLM 失败(重试后仍无输出):{exc2}"}
    except Exception as exc:
        logger.warning("suggest_importance_experience call failed: %s", exc)
        return {"candidate_text": "", "duplicate_of": None,
                "reason": f"LLM 失败:{exc}"}
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("suggest_importance_experience JSON parse failed: %s | raw=%s",
                       exc, raw[:400])
        return {"candidate_text": "", "duplicate_of": None,
                "reason": "LLM 返回不是合法 JSON"}
    if not isinstance(parsed, dict):
        return {"candidate_text": "", "duplicate_of": None,
                "reason": "LLM 返回结构非对象"}
    dup = parsed.get("duplicate_of")
    if isinstance(dup, str):
        dup = dup.strip() or None
        if dup and existing and not any(e.get("id") == dup for e in existing):
            dup = None
    else:
        dup = None
    text = (parsed.get("candidate_text") or "").strip().strip("「」\"'").strip()
    if text.startswith(("- ", "• ", "* ")):
        text = text[2:].strip()
    return {
        "candidate_text": text[:240],
        "duplicate_of": dup,
        "reason": (parsed.get("reason") or "").strip()[:200],
    }


# How many experiences per LLM call. Output is now "changes only"
# (merges + drops; unmentioned = keep), which shrinks the required
# response size a lot — so we can fit more per call without blowing
# the reasoning-model budget. 25 items per chunk empirically completes
# under a 16k budget; a 24k retry catches the rare over-reasoner.
_ORGANIZE_CHUNK_SIZE = 25


def _distill_organize_one_chunk(experiences: List[Dict[str, str]]) -> Dict:
    """Run the organize LLM call on ONE chunk of experiences.
    Returns {actions: [...]} on success, {actions: [], error: "..."} on
    failure. Never raises. Kept private — callers should go through
    distill_organize_experiences which handles chunking + merging.

    Output contract: the LLM lists ONLY the ids to merge or drop;
    every id it doesn't mention is treated as an implicit keep by
    distill_organize_experiences. This shrinks response size (fewer
    ids to echo, no reason strings on kept items) and — more
    importantly — means the LLM never has to enumerate 20+ ids just
    to say "keep them all". Reasoning-token burn drops accordingly.
    """
    if not experiences:
        return {"actions": []}
    lines = [f"[{e.get('id')}] {(e.get('text') or '').strip()}" for e in experiences]
    corpus = "\n".join(lines)
    system_msg = (
        "你是「经验条目整理助手」,任务是**精简**用户的邮件分类经验列表。\n"
        "核心原则:**宁愿丢失一些经验也要显著减少条目数**。用户已经明确表示"
        "偏好更精简的列表,而不是「更全」的列表。\n"
        "执行策略:\n"
        "1. 只要两条经验的适用场景有明显重叠 → 合并成一条更通用的表述(merge)\n"
        "2. 若某条经验被另一条(哪怕更宽泛的)大致覆盖 → 直接删除较弱的一条(drop)\n"
        "3. 意思相反 / 存在冲突 → 只保留更具体或更近期的一条,另一条 drop\n"
        "4. 只有当一条经验独一无二、没有任何近似邻居时才让它保留\n"
        "5. **合并 3~4 条相近经验为 1 条,是非常受欢迎的做法**\n"
        "6. 拿不准是否重要 → 倾向 drop(用户已授权你偏激进)"
    )
    user_msg = (
        f"以下是当前经验列表({len(experiences)} 条),格式为 [id] 文本:\n\n"
        f"{corpus}\n\n"
        "请返回严格 JSON,顶级只有一个键 `actions`,值为数组。**只列出要合并或"
        "删除的动作;未在输出里出现的 id 会自动保留原样,不需要你写 keep**。\n\n"
        "示例:\n"
        "```\n"
        "{\n"
        '  "actions": [\n'
        '    {"type":"merge", "from_ids":["x_1","x_2","x_3"], "new_text":"合并后一句 ≤80 字"},\n'
        '    {"type":"drop",  "ids":["x_5"], "reason":"被 x_6 覆盖"}\n'
        "  ]\n"
        "}\n"
        "```\n"
        "❌ 不要输出 keep 动作,不要用 `{merge:[...], drop:[...]}` 这种把 type "
        "当作顶级键的形式。\n"
        "❌ 不要输出 JSON 以外的任何字符或 Markdown 代码块。\n"
        "❌ merge 至少要合并 2 条(from_ids 长度 ≥ 2);drop 每次可以 1 条起。\n"
        "✅ 尽量多合并、多删除。目标是让最终条目数至少减少 30%。"
    )

    def _run(max_tokens: int, timeout: int) -> str:
        payload = {
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        return chat_completion(payload, timeout=timeout).strip()

    # v4-flash reasoning-model budget: start at 16k; on empty-content
    # (reasoning burned the whole budget) escalate to 24k. This mirrors
    # the pattern in suggest_recategorize_experience.
    try:
        raw = _run(max_tokens=16000, timeout=180)
    except ValueError as exc:
        if "empty content" not in str(exc):
            logger.warning("organize chunk failed (%d items): %s", len(experiences), exc)
            return {"actions": [], "error": str(exc)}
        logger.warning("organize chunk (%d items): empty at 16k, retry at 24k", len(experiences))
        try:
            raw = _run(max_tokens=24000, timeout=300)
        except Exception as exc2:
            logger.warning("organize chunk retry failed: %s", exc2)
            return {"actions": [], "error": f"LLM 在扩大预算后仍无输出: {exc2}"}
    except Exception as exc:
        logger.warning("organize chunk failed (%d items): %s", len(experiences), exc)
        return {"actions": [], "error": str(exc)}
    # Strip accidental code fences.
    if raw.startswith("```"):
        lines2 = raw.split("\n")
        if lines2 and lines2[0].startswith("```"):
            lines2 = lines2[1:]
        if lines2 and lines2[-1].strip().startswith("```"):
            lines2 = lines2[:-1]
        raw = "\n".join(lines2).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("organize chunk JSON parse failed: %s | raw=%s", exc, raw[:400])
        return {"actions": [], "error": "JSON parse failed"}
    if not isinstance(parsed, dict):
        return {"actions": [], "error": "invalid response shape"}

    # Preferred shape: {"actions": [...]}. Tolerate {"merge":[...],
    # "drop":[...]} legacy shape too — see previous version's rationale.
    # `keep` is now ignored (implicit); we don't need to accept it.
    if isinstance(parsed.get("actions"), list):
        return {"actions": parsed["actions"]}
    canonical: List[Dict] = []
    for kind in ("drop", "merge"):
        items = parsed.get(kind)
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            entry = {**it, "type": kind}
            canonical.append(entry)
    if canonical or "actions" in parsed:
        # Empty actions list is a valid "nothing to change" verdict.
        return {"actions": canonical}
    return {"actions": [], "error": "invalid response shape"}


def distill_organize_experiences(
    experiences: List[Dict[str, str]],
) -> Dict:
    """Ask the LLM to look over the current experience corpus and propose a
    dedup / conflict-resolution plan. Returns a dict of shape:

        {
          "actions": [
            {"type": "keep",  "ids": ["x_..."], "text": "…"},
            {"type": "drop",  "ids": ["x_...", …], "reason": "冗余"},
            {"type": "merge", "from_ids": [...], "new_text": "…"},
          ]
        }

    Contract: every input experience id must appear in exactly one action's
    ids / from_ids list — the plan partitions the input. On LLM failure or
    malformed output for a chunk, the ids in that chunk fall through as
    implicit keep so no data is ever lost.

    Large corpora are split into `_ORGANIZE_CHUNK_SIZE`-item chunks and
    processed sequentially; the caller sees one merged plan. Cross-chunk
    duplicates are NOT caught in a single run (a re-run on the smaller
    result set will catch them) — the tradeoff for having the whole
    operation succeed on any corpus size.

    `experiences` items are shape {"id": "x_...", "text": "…"}.
    """
    if not experiences:
        return {"actions": []}
    input_ids = [str(e["id"]) for e in experiences if e.get("id")]
    input_id_set = set(input_ids)
    seen: set = set()
    merged: List[Dict] = []
    partial_errors: List[str] = []

    for i in range(0, len(experiences), _ORGANIZE_CHUNK_SIZE):
        chunk = experiences[i : i + _ORGANIZE_CHUNK_SIZE]
        chunk_ids = {str(e["id"]) for e in chunk if e.get("id")}
        result = _distill_organize_one_chunk(chunk)
        if result.get("error"):
            # This chunk failed — collect its ids as implicit keeps so
            # nothing gets lost, and remember the error to surface in UI.
            partial_errors.append(result["error"])
            forgotten = [cid for cid in chunk_ids if cid not in seen]
            if forgotten:
                merged.append({"type": "keep", "ids": forgotten, "text": ""})
                for cid in forgotten:
                    seen.add(cid)
            continue

        for a in result.get("actions", []):
            if not isinstance(a, dict):
                continue
            t = a.get("type")
            if t == "keep":
                # Only accept ids that belong to THIS chunk (LLM may have
                # hallucinated) and haven't been assigned to a prior action.
                ids = [i2 for i2 in (a.get("ids") or [])
                       if isinstance(i2, str) and i2 in chunk_ids and i2 not in seen]
                for cid in ids:
                    seen.add(cid)
                if ids:
                    merged.append({"type": "keep", "ids": ids, "text": a.get("text") or ""})
            elif t == "drop":
                ids = [i2 for i2 in (a.get("ids") or [])
                       if isinstance(i2, str) and i2 in chunk_ids and i2 not in seen]
                for cid in ids:
                    seen.add(cid)
                if ids:
                    merged.append({"type": "drop", "ids": ids, "reason": (a.get("reason") or "").strip()})
            elif t == "merge":
                from_ids = [i2 for i2 in (a.get("from_ids") or [])
                            if isinstance(i2, str) and i2 in chunk_ids and i2 not in seen]
                new_text = (a.get("new_text") or "").strip()
                if len(from_ids) >= 2 and new_text:
                    for cid in from_ids:
                        seen.add(cid)
                    merged.append({"type": "merge", "from_ids": from_ids, "new_text": new_text[:240]})

    # Any id the LLM forgot across all chunks → implicit keep so we never lose it.
    forgotten = [i for i in input_id_set if i not in seen]
    if forgotten:
        merged.append({"type": "keep", "ids": forgotten, "text": ""})

    out: Dict = {"actions": merged}
    if partial_errors and not any(a["type"] in ("drop", "merge") for a in merged):
        # All chunks that reported errors returned nothing actionable —
        # surface the first error so the user knows nothing will happen.
        out["error"] = partial_errors[0]
    elif partial_errors:
        # Some chunks succeeded, some failed. Report as an informational
        # note so the user knows a re-run may find more.
        out["note"] = f"{len(partial_errors)} 个批次 LLM 未返回结果,已跳过并保留原样。稍后可重试。"
    return out


# ── One-click organize for user prompts ──────────────────────────────
# Same 3-way (merge / drop / implicit-keep) contract as
# `distill_organize_experiences`, but prompts carry two extra fields
# that shape merges:
#
#   `name`           — optional short slug shown as a chip in the UI
#   `target_folder`  — the folder the prompt routes into. Two prompts
#                       with DIFFERENT target_folders solve DIFFERENT
#                       classification problems, so the LLM must never
#                       merge across target groups. We enforce this
#                       both in the prompt instructions AND in the
#                       downstream apply step (validation).


def _distill_organize_prompts_one_chunk(prompts: List[Dict[str, str]]) -> Dict:
    """Run the organize LLM call on ONE chunk of prompts.
    Same output contract as _distill_organize_one_chunk (experiences):
    only merge/drop; unmentioned = implicit keep.

    Extra semantic: merges must NOT cross target_folder boundaries.
    Rejected merges become no-ops in distill_organize_prompts.
    """
    if not prompts:
        return {"actions": []}
    lines = []
    for p in prompts:
        pid = p.get("id") or ""
        name = (p.get("name") or "").strip() or "(无名)"
        tgt = (p.get("target_folder") or "").strip() or "(无目标文件夹)"
        text = (p.get("text") or "").strip()
        lines.append(f"[{pid}] name={name} · target={tgt}\n    {text}")
    corpus = "\n\n".join(lines)
    system_msg = (
        "你是「提示条目整理助手」,任务是**精简**用户为邮件分类维护的提示列表。\n"
        "核心原则:**宁愿丢失一些提示也要显著减少条目数**。用户明确偏好更精简"
        "的列表。\n"
        "执行策略:\n"
        "1. 只要两条提示的适用场景明显重叠且 **target 完全相同** → 合并成一条更"
        "通用的表述(merge)\n"
        "2. 若某条提示被另一条(哪怕更宽泛的、且 target 相同)大致覆盖 → "
        "直接删除较弱的一条(drop)\n"
        "3. 意思相反 / 存在冲突且 target 相同 → 只保留更具体或更近期的一条,"
        "另一条 drop\n"
        "4. **⚠️ 不同 target 的提示绝对不能合并** —— 它们对应不同的分类目标,"
        "合并会造成分类错乱\n"
        "5. 只有当一条提示独一无二、没有任何近似邻居时才让它保留\n"
        "6. **合并 3~4 条相近提示为 1 条,是非常受欢迎的做法**(前提是 target 一致)\n"
        "7. 拿不准是否重要 → 倾向 drop(用户已授权你偏激进)"
    )
    user_msg = (
        f"以下是当前提示列表({len(prompts)} 条),格式为 `[id] name=X · target=Y \\n text`:\n\n"
        f"{corpus}\n\n"
        "请返回严格 JSON,顶级只有一个键 `actions`,值为数组。**只列出要合并或"
        "删除的动作;未在输出里出现的 id 会自动保留原样,不需要你写 keep**。\n\n"
        "示例:\n"
        "```\n"
        "{\n"
        '  "actions": [\n'
        '    {"type":"merge", "from_ids":["p_1","p_2","p_3"],\n'
        '     "new_name":"合并后的简短名(≤20 字)",\n'
        '     "new_text":"合并后一句 ≤500 字",\n'
        '     "target_folder":"必须与被合并的所有 from_ids 一致"},\n'
        '    {"type":"drop",  "ids":["p_5"], "reason":"被 p_6 覆盖"}\n'
        "  ]\n"
        "}\n"
        "```\n"
        "❌ 不要输出 keep 动作。\n"
        "❌ 不要输出 JSON 以外的任何字符或 Markdown 代码块。\n"
        "❌ merge 至少要合并 2 条(from_ids 长度 ≥ 2);drop 每次可以 1 条起。\n"
        "❌ merge 的 from_ids 中所有条目必须 target 完全相同,否则你的合并会被"
        "系统拒绝。\n"
        "✅ 尽量多合并、多删除。目标是让最终条目数至少减少 30%。"
    )

    def _run(max_tokens: int, timeout: int) -> str:
        payload = {
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        return chat_completion(payload, timeout=timeout).strip()

    try:
        raw = _run(max_tokens=16000, timeout=180)
    except ValueError as exc:
        if "empty content" not in str(exc):
            logger.warning("organize-prompts chunk failed (%d items): %s",
                           len(prompts), exc)
            return {"actions": [], "error": str(exc)}
        logger.warning("organize-prompts chunk (%d items): empty at 16k, retry at 24k",
                       len(prompts))
        try:
            raw = _run(max_tokens=24000, timeout=300)
        except Exception as exc2:
            logger.warning("organize-prompts chunk retry failed: %s", exc2)
            return {"actions": [], "error": f"LLM 在扩大预算后仍无输出: {exc2}"}
    except Exception as exc:
        logger.warning("organize-prompts chunk failed (%d items): %s",
                       len(prompts), exc)
        return {"actions": [], "error": str(exc)}
    if raw.startswith("```"):
        lines2 = raw.split("\n")
        if lines2 and lines2[0].startswith("```"):
            lines2 = lines2[1:]
        if lines2 and lines2[-1].strip().startswith("```"):
            lines2 = lines2[:-1]
        raw = "\n".join(lines2).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("organize-prompts chunk JSON parse failed: %s | raw=%s",
                       exc, raw[:400])
        return {"actions": [], "error": "JSON parse failed"}
    if not isinstance(parsed, dict):
        return {"actions": [], "error": "invalid response shape"}
    if isinstance(parsed.get("actions"), list):
        return {"actions": parsed["actions"]}
    canonical: List[Dict] = []
    for kind in ("drop", "merge"):
        items = parsed.get(kind)
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            entry = {**it, "type": kind}
            canonical.append(entry)
    if canonical or "actions" in parsed:
        return {"actions": canonical}
    return {"actions": [], "error": "invalid response shape"}


def distill_organize_prompts(prompts: List[Dict[str, str]]) -> Dict:
    """Aggregate + normalise a full-corpus organize plan for prompts.
    Same contract as `distill_organize_experiences` but each action may
    carry a `new_name` (short slug) and a `target_folder` (preserved
    from the group being merged). Cross-target merges are dropped
    silently — they would be rejected downstream anyway and there's no
    graceful "half-merge" we can do."""
    if not prompts:
        return {"actions": []}
    input_ids = [str(p["id"]) for p in prompts if p.get("id")]
    input_id_set = set(input_ids)
    tgt_by_id = {
        str(p["id"]): (p.get("target_folder") or "").strip()
        for p in prompts if p.get("id")
    }
    seen: set = set()
    merged: List[Dict] = []
    partial_errors: List[str] = []
    rejected_cross_target = 0

    for i in range(0, len(prompts), _ORGANIZE_CHUNK_SIZE):
        chunk = prompts[i : i + _ORGANIZE_CHUNK_SIZE]
        chunk_ids = {str(p["id"]) for p in chunk if p.get("id")}
        result = _distill_organize_prompts_one_chunk(chunk)
        if result.get("error"):
            partial_errors.append(result["error"])
            forgotten = [cid for cid in chunk_ids if cid not in seen]
            if forgotten:
                merged.append({"type": "keep", "ids": forgotten, "text": ""})
                for cid in forgotten:
                    seen.add(cid)
            continue

        for a in result.get("actions", []):
            if not isinstance(a, dict):
                continue
            t = a.get("type")
            if t == "keep":
                # Tolerate a stray keep even though we ask not to.
                ids = [i2 for i2 in (a.get("ids") or [])
                       if isinstance(i2, str) and i2 in chunk_ids and i2 not in seen]
                for cid in ids:
                    seen.add(cid)
                if ids:
                    merged.append({"type": "keep", "ids": ids, "text": a.get("text") or ""})
            elif t == "drop":
                ids = [i2 for i2 in (a.get("ids") or [])
                       if isinstance(i2, str) and i2 in chunk_ids and i2 not in seen]
                for cid in ids:
                    seen.add(cid)
                if ids:
                    merged.append({
                        "type": "drop",
                        "ids": ids,
                        "reason": (a.get("reason") or "").strip(),
                    })
            elif t == "merge":
                from_ids = [i2 for i2 in (a.get("from_ids") or [])
                            if isinstance(i2, str) and i2 in chunk_ids and i2 not in seen]
                new_text = (a.get("new_text") or "").strip()
                new_name = (a.get("new_name") or "").strip()
                if len(from_ids) < 2 or not new_text:
                    continue
                # Enforce single-target: all from_ids must share a target.
                targets = {tgt_by_id.get(fid, "") for fid in from_ids}
                if len(targets) > 1:
                    rejected_cross_target += 1
                    continue
                target = next(iter(targets)) if targets else ""
                for cid in from_ids:
                    seen.add(cid)
                merged.append({
                    "type": "merge",
                    "from_ids": from_ids,
                    "new_text": new_text[:2000],
                    "new_name": new_name[:48],
                    "target_folder": target,
                })

    forgotten = [i for i in input_id_set if i not in seen]
    if forgotten:
        merged.append({"type": "keep", "ids": forgotten, "text": ""})

    out: Dict = {"actions": merged}
    if partial_errors and not any(a["type"] in ("drop", "merge") for a in merged):
        out["error"] = partial_errors[0]
    elif partial_errors:
        out["note"] = f"{len(partial_errors)} 个批次 LLM 未返回结果,已跳过并保留原样。稍后可重试。"
    if rejected_cross_target:
        note_extra = f"{rejected_cross_target} 组跨 target 合并被拒(不同分类目标不能合并)。"
        out["note"] = (out.get("note") + " " + note_extra) if out.get("note") else note_extra
    return out


# ── AI-powered email search ──────────────────────────────────────────
# Given a natural-language query, judge whether a single email matches.
# Wrapped by the search-task machinery in main.py which iterates over
# the target date-range one email at a time. Returns a dict with a
# strict boolean `match` and a short reason — never raises.


def judge_email_matches_query(
    *,
    query: str,
    from_email: str = "",
    to_email: str = "",
    cc_email: str = "",
    subject: str = "",
    body: str = "",
    body_char_cap: int = 1200,
    owner_email: str = "",
) -> Dict:
    """Ask the LLM whether this email matches the user's search intent.
    Returns {"match": bool, "reason": str}. Best-effort — on any failure
    returns {"match": False, "reason": "<error>"} so the caller can
    keep iterating."""
    body_excerpt = (body or "")[:max(0, body_char_cap)]

    system_msg = (
        "你是一名邮件语义检索助手。用户给出一段自然语言的检索需求;你需要判断"
        "一封具体邮件是否满足这个需求。判断标准要**严格**:只有当邮件的主题或"
        "正文**明确**涉及用户所描述的话题时才判为匹配。"
        "如果邮件只是与话题擦边、含糊、或只出现零星关键词而没有实际相关内容,"
        "一律判为不匹配。"
    )
    context_lines = []
    if owner_email:
        context_lines.append(f"（当前账号邮箱: {owner_email}）")
    context_lines.append(f"用户检索需求: {query.strip()}")
    context_lines.append("")
    context_lines.append("待判断的邮件:")
    context_lines.append(f"发件人: {from_email or '(unknown)'}")
    context_lines.append(f"收件人(To): {to_email or '(unknown)'}")
    context_lines.append(f"抄送(Cc): {cc_email or '(无)'}")
    context_lines.append(f"主题: {subject or '(empty)'}")
    context_lines.append(f"正文:\n{body_excerpt or '(无)'}")
    user_msg = "\n".join(context_lines) + (
        "\n\n请返回**严格 JSON**,顶级只有两个字段:\n"
        '{"match": true|false, "reason": "<20 字内中文说明,若匹配请指出邮件里最能支持这一判断的片段>"}\n'
        "❌ 不要输出 JSON 以外的字符,不要 Markdown 代码块。"
    )

    def _run(max_tokens: int, timeout: int) -> str:
        payload = {
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        return chat_completion(payload, timeout=timeout).strip()

    try:
        raw = _run(max_tokens=4000, timeout=60)
    except ValueError as exc:
        if "empty content" not in str(exc):
            return {"match": False, "reason": f"LLM 失败:{exc}"}
        try:
            raw = _run(max_tokens=8000, timeout=120)
        except Exception as exc2:
            return {"match": False, "reason": f"LLM 失败(扩预算后仍无输出):{exc2}"}
    except Exception as exc:
        return {"match": False, "reason": f"LLM 失败:{exc}"}
    if raw.startswith("```"):
        lines2 = raw.split("\n")
        if lines2 and lines2[0].startswith("```"):
            lines2 = lines2[1:]
        if lines2 and lines2[-1].strip().startswith("```"):
            lines2 = lines2[:-1]
        raw = "\n".join(lines2).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"match": False, "reason": "LLM 返回不是合法 JSON"}
    if not isinstance(parsed, dict):
        return {"match": False, "reason": "LLM 返回结构非对象"}
    match_raw = parsed.get("match")
    if isinstance(match_raw, bool):
        matched = match_raw
    elif isinstance(match_raw, str):
        matched = match_raw.strip().lower() in {"true", "1", "yes", "y"}
    else:
        matched = False
    reason = (parsed.get("reason") or "").strip()[:200]
    return {"match": matched, "reason": reason}


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


def _post_deepseek(req: urllib.request.Request, timeout: int) -> str:
    """POST once respecting the system HTTP proxy; on the specific class of
    failures that indicate the proxy itself is broken (Tunnel CONNECT 5xx,
    "Cannot connect to proxy"), retry ONCE with the direct opener.

    Rationale: corporate networks legitimately require going through a
    proxy, so we still try that first. But a common home-user footgun is
    installing an HTTP debug tool (MacPacket, Charles, Proxyman) that
    registers itself as the system proxy in System Settings → Network →
    Proxies — those tools happily terminate localhost CONNECT but often
    refuse or 5xx CONNECTs to arbitrary remote endpoints, taking out the
    entire LLM path. The direct-fallback keeps DeepSeek reachable in that
    scenario without breaking corporate users who need the proxy path."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        msg = str(exc).lower()
        looks_like_proxy_failure = (
            "tunnel connection failed" in msg
            or "cannot connect to proxy" in msg
            or "proxyerror" in msg
        )
        if not looks_like_proxy_failure:
            raise
        logger.warning(
            "System HTTP proxy failed for DeepSeek call (%s); retrying direct.",
            exc,
        )
        with _DIRECT_HTTP.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")


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
    raw = _post_deepseek(req, timeout or _TIMEOUT_SEC)
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
