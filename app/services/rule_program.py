# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Deterministic, AST-evaluated classification rules.

The user writes a rule in natural language. We pass that NL to DeepSeek
once at save-time, which translates it into a JSON AST. The AST is what we
persist and what we evaluate against each incoming email — runtime
classification therefore has zero LLM dependency, no network, and produces
stable, auditable decisions.

AST shape
---------
Branch nodes:
  {"type": "and", "children": [<node>, ...]}
  {"type": "or",  "children": [<node>, ...]}
  {"type": "not", "child": <node>}

Leaf nodes (over the email fields below):
  {"type": "contains",   "field": <field>, "value":   <str>, "case_insensitive": true}
  {"type": "equals",     "field": <field>, "value":   <str>, "case_insensitive": true}
  {"type": "startswith", "field": <field>, "value":   <str>, "case_insensitive": true}
  {"type": "endswith",   "field": <field>, "value":   <str>, "case_insensitive": true}
  {"type": "regex",      "field": <field>, "pattern": <re>,  "case_insensitive": true}

Allowed fields: from / to / cc / subject / body.

Field semantics at evaluation time:
  - from: primary sender's email address (e.g. "alice@example.com")
  - to:   primary recipient's email address (first To in the header)
  - cc:   raw Cc header text (decoded), empty when there is no Cc
  - subject / body: as expected
"""

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------- names & references ----------

# A name token: starts with a letter; ASCII letters/digits/underscore/hyphen
# inside; 1-48 chars total. Stricter than a generic identifier so names look
# distinct from random labels.
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,47}$")
# @ref inside NL: explicit braces avoid colliding with email addresses
# ("@example.com") and Twitter-style mentions written in the description.
# Users write @{rule_name}; the regex captures the bare name.
REF_RE = re.compile(r"@\{([A-Za-z][A-Za-z0-9_\-]{0,47})\}")


def parse_refs(nl_text: str) -> List[str]:
    """Returns the ordered, de-duplicated list of @name tokens in the NL."""
    seen: Set[str] = set()
    out: List[str] = []
    for m in REF_RE.finditer(nl_text or ""):
        nm = m.group(1)
        if nm not in seen:
            seen.add(nm)
            out.append(nm)
    return out


def slugify_name(text: str, existing: Iterable[str], fallback_prefix: str = "rule") -> str:
    """Generate a kebab-style name from arbitrary text, avoiding collisions
    with `existing`. Used to auto-assign a name when the user didn't provide
    one. Keeps a-z0-9_- only; collapses runs; lowercases; truncates."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "-", (text or "").strip().lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-_")
    cleaned = cleaned[:32] or fallback_prefix
    if cleaned not in existing:
        return cleaned
    i = 2
    while f"{cleaned}-{i}" in existing:
        i += 1
    return f"{cleaned}-{i}"


def expand_refs(
    nl_text: str,
    lookup: Dict[str, str],
    *,
    self_name: str = "",
    max_depth: int = 8,
) -> Tuple[str, List[str]]:
    """Recursively replace each @name in `nl_text` with the referenced
    rule/prompt's own NL text (wrapped in parentheses for grouping). Returns
    (expanded_text, ordered_dependencies_from_root_call_only).

    `self_name` seeds the visiting stack so a rule that references its own
    name is rejected as a cycle.

    Raises ValueError with a Chinese message on:
      - reference to an undefined name
      - circular reference
      - expansion depth exceeded (defensive)
    """
    top_level_refs: List[str] = []
    seen_top: Set[str] = set()

    def go(text: str, visiting: List[str], depth: int) -> str:
        if depth > max_depth:
            raise ValueError(f"@引用展开深度超过 {max_depth} 层，疑似递归过深")

        def replace(match: re.Match) -> str:
            name = match.group(1)
            if depth == 0 and name not in seen_top:
                seen_top.add(name)
                top_level_refs.append(name)
            if name in visiting:
                chain = " → ".join(visiting + [name])
                raise ValueError(f"检测到循环引用：{chain}")
            if name not in lookup:
                raise ValueError(f"引用了不存在的名字：@{name}")
            sub = lookup[name]
            expanded = go(sub, visiting + [name], depth + 1)
            return f"（{expanded}）"

        return REF_RE.sub(replace, text)

    initial_visiting = [self_name] if self_name else []
    expanded = go(nl_text or "", initial_visiting, 0)
    return expanded, top_level_refs


ALLOWED_FIELDS = ("from", "to", "cc", "subject", "body")
LEAF_OPS = ("contains", "equals", "startswith", "endswith", "regex")
BRANCH_OPS = ("and", "or", "not")
MAX_DEPTH = 8
MAX_NODES = 64


# ---------- validation ----------

def validate_program(program: Any) -> List[str]:
    """Returns a list of human-readable errors. Empty list ⇒ program is well
    formed and safe to evaluate. Defends against deep recursion bombs and
    invalid regex patterns."""
    counter = {"n": 0}

    def visit(node: Any, depth: int) -> List[str]:
        if depth > MAX_DEPTH:
            return [f"程序嵌套层级超过 {MAX_DEPTH}"]
        counter["n"] += 1
        if counter["n"] > MAX_NODES:
            return [f"程序节点数超过 {MAX_NODES}"]
        if not isinstance(node, dict):
            return [f"节点必须是对象，得到：{type(node).__name__}"]
        t = node.get("type")
        if t in ("and", "or"):
            children = node.get("children")
            if not isinstance(children, list) or not children:
                return [f"{t} 节点必须有非空 children 列表"]
            errs: List[str] = []
            for c in children:
                errs.extend(visit(c, depth + 1))
            return errs
        if t == "not":
            c = node.get("child")
            if c is None:
                return ["not 节点必须有 child"]
            return visit(c, depth + 1)
        if t in LEAF_OPS:
            field = node.get("field")
            if field not in ALLOWED_FIELDS:
                return [f"叶子节点字段必须是 {ALLOWED_FIELDS}，得到 {field!r}"]
            if t == "regex":
                pattern = node.get("pattern")
                if not isinstance(pattern, str) or not pattern:
                    return ["regex 节点缺少非空 pattern"]
                try:
                    re.compile(pattern)
                except re.error as exc:
                    return [f"正则表达式无效：{exc}"]
            else:
                value = node.get("value")
                if not isinstance(value, str):
                    return [f"{t} 节点缺少字符串 value"]
            return []
        return [f"未知节点类型：{t!r}"]

    return visit(program, 0)


# ---------- evaluation ----------

def evaluate_program(program: Any, email: Dict[str, Any]) -> bool:
    """Pure interpreter — no eval/exec, no network. `email` is a dict with
    string fields {from, to, subject, body}. Missing fields default to ""."""
    if not isinstance(program, dict):
        return False
    t = program.get("type")
    if t == "and":
        for c in program.get("children") or []:
            if not evaluate_program(c, email):
                return False
        return True
    if t == "or":
        for c in program.get("children") or []:
            if evaluate_program(c, email):
                return True
        return False
    if t == "not":
        return not evaluate_program(program.get("child") or {}, email)
    if t in LEAF_OPS:
        field = program.get("field")
        if field not in ALLOWED_FIELDS:
            return False
        haystack = str(email.get(field) or "")
        ci = bool(program.get("case_insensitive", True))
        if t == "regex":
            pattern = program.get("pattern") or ""
            flags = re.IGNORECASE if ci else 0
            try:
                return re.search(pattern, haystack, flags) is not None
            except re.error:
                return False
        value = str(program.get("value", ""))
        if ci:
            haystack = haystack.lower()
            value = value.lower()
        if t == "contains":
            return value in haystack
        if t == "equals":
            return value == haystack
        if t == "startswith":
            return haystack.startswith(value)
        if t == "endswith":
            return haystack.endswith(value)
    return False


# ---------- pseudo-code rendering ----------

_FIELD_DISPLAY = {
    "from": "email.from",
    "to": "email.to",
    "cc": "email.cc",
    "subject": "email.subject",
    "body": "email.body",
}


def render_pseudo_code(program: Any) -> str:
    """Render the AST as Python-flavored pseudo-code, e.g.

        if (("发票" in email.subject)
            and (email.from.endswith("@example.com"))):
            category = TARGET

    Purely a human-readable preview; never executed."""
    inner = _render_node(program, indent="    ")
    return (
        "def matches(email):\n"
        f"    return {inner}\n"
    )


def _render_node(node: Any, indent: str = "") -> str:
    if not isinstance(node, dict):
        return "False"
    t = node.get("type")
    if t == "and":
        parts = [_render_node(c, indent) for c in (node.get("children") or [])]
        if not parts:
            return "True"
        if len(parts) == 1:
            return parts[0]
        joined = (" and\n" + indent + " " * 4).join(parts)
        return f"(\n{indent}    {joined}\n{indent})"
    if t == "or":
        parts = [_render_node(c, indent) for c in (node.get("children") or [])]
        if not parts:
            return "False"
        if len(parts) == 1:
            return parts[0]
        joined = (" or\n" + indent + " " * 4).join(parts)
        return f"(\n{indent}    {joined}\n{indent})"
    if t == "not":
        return "not (" + _render_node(node.get("child") or {}, indent) + ")"
    field = _FIELD_DISPLAY.get(node.get("field"), str(node.get("field")))
    if t == "regex":
        return f're.search({json.dumps(node.get("pattern") or "", ensure_ascii=False)}, {field})'
    value = json.dumps(node.get("value", ""), ensure_ascii=False)
    if t == "contains":
        return f"({value} in {field})"
    if t == "equals":
        return f"({field} == {value})"
    if t == "startswith":
        return f"{field}.startswith({value})"
    if t == "endswith":
        return f"{field}.endswith({value})"
    return "False"


# ---------- NL → AST compilation (DeepSeek) ----------

_COMPILE_SYSTEM_PROMPT = """你是一个邮件分类规则编译器。把用户的自然语言规则翻译成一段严格的 JSON 程序，便于服务器在本地执行（不再依赖大模型）。

允许的字段：
  from      —— 发件人邮箱地址（如 "alice@example.com"）
  to        —— 主收件人邮箱地址（仅第一个）
  cc        —— Cc 抄送原始头部文本（含姓名与地址；没有抄送时为空字符串 ""）
  subject   —— 邮件主题
  body      —— 邮件正文

判断"是否有抄送"用 contains(cc, "@") 或 equals(cc, "")；
判断"抄送中包含某人"用 contains(cc, "person@domain.com")。

允许的节点（type 是字符串字面量）：
  叶子节点（必须有 field；除 regex 外必须有 value）：
    {"type": "contains",   "field": <field>, "value":   <str>, "case_insensitive": true}
    {"type": "equals",     "field": <field>, "value":   <str>, "case_insensitive": true}
    {"type": "startswith", "field": <field>, "value":   <str>, "case_insensitive": true}
    {"type": "endswith",   "field": <field>, "value":   <str>, "case_insensitive": true}
    {"type": "regex",      "field": <field>, "pattern": <re>,  "case_insensitive": true}
  组合节点：
    {"type": "and",  "children": [<node>, ...]}    # 全部满足
    {"type": "or",   "children": [<node>, ...]}    # 任一满足
    {"type": "not",  "child": <node>}              # 取反

约束：
- 整棵树的节点数不超过 64，嵌套不超过 8 层；
- 不要凭空增加未提及的条件；用户给出的就是规则的全部；
- 不要把目标文件夹（target_folder）放进 program；调用方会单独保存它；
- "包含" / "出现" / "含有" → contains
- "等于" / "是" → equals
- "以…开头" → startswith；"以…结尾" / "来自 xxx 域名" → endswith（如 "@xxx.com"）
- 对邮箱后缀过滤（"发件人来自 a.com"），优先用 endswith("@a.com")
- 默认 case_insensitive: true，除非用户明确要求大小写敏感
- 如果用户描述含糊或无法翻译，把 explanation 写清楚你的最佳猜测；不要拒绝输出

输出（严格 JSON，不要带 markdown 包装、不要解释，仅输出对象本身）：
{
  "explanation": "用中文简短说明你对这条规则的理解（≤80 字）",
  "program": <根节点对象>,
  "code_preview": "用 Python 伪代码展示等价规则（多行字符串，便于人类核对）"
}
"""


def compile_from_nl(
    nl_text: str,
    *,
    name_lookup: Optional[Dict[str, str]] = None,
    self_name: str = "",
) -> Dict[str, Any]:
    """Translate the user's natural-language rule into an executable program.

    Steps:
      1. Expand any @name references in the NL recursively (cycle-checked
         against `self_name`). All references must resolve against
         `name_lookup`, which maps the names of OTHER rules/prompts (in the
         same account) to their NL text.
      2. Send the expanded NL to DeepSeek and parse a strict JSON AST.
      3. Validate the AST.

    Returns:
        {
          "program": <ast>,
          "explanation": str,
          "code_preview": str,
          "expanded_nl": str,
          "refs": [<top-level @names actually used>],
        }

    Raises ValueError for bad input (empty NL, missing/cyclic refs).
    Raises RuntimeError for upstream failures (API key, DeepSeek HTTP/JSON).
    """
    from app.services.spam_filter import chat_completion  # local: avoid cycle at import-time

    text = (nl_text or "").strip()
    if not text:
        raise ValueError("自然语言规则不能为空")

    lookup = dict(name_lookup or {})
    # If the rule references itself by its own name, the expander treats it
    # as an immediate cycle via the self_name seed; no extra handling needed.
    expanded, refs = expand_refs(text, lookup, self_name=self_name or "")

    payload = {
        "messages": [
            {"role": "system", "content": _COMPILE_SYSTEM_PROMPT},
            {"role": "user", "content": expanded},
        ],
        "temperature": 0.0,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }

    try:
        content = chat_completion(payload, timeout=30)
    except RuntimeError as exc:
        if str(exc) == "no api key":
            raise RuntimeError(
                "尚未配置 DeepSeek API Key，无法把自然语言翻译为程序。请到设置页填写。"
            )
        raise RuntimeError(f"调用 DeepSeek 失败：{exc}")
    except Exception as exc:
        raise RuntimeError(f"调用 DeepSeek 失败：{exc.__class__.__name__}: {exc}")

    try:
        parsed = json.loads(content)
    except Exception as exc:
        logger.warning("rule_program: malformed JSON from LLM: %s | raw=%s", exc, content[:500])
        raise RuntimeError("大模型返回的内容不是合法 JSON，请稍后重试或调整描述")

    program = parsed.get("program")
    explanation = str(parsed.get("explanation") or "").strip()
    code_preview = str(parsed.get("code_preview") or "").strip()

    errs = validate_program(program)
    if errs:
        joined = "；".join(errs)
        logger.warning("rule_program: invalid AST: %s | program=%s", joined, program)
        raise RuntimeError(f"生成的程序不合法：{joined}")

    if not code_preview:
        code_preview = render_pseudo_code(program)
    if not explanation:
        explanation = "（未提供解释，请直接看下方代码）"

    return {
        "program": program,
        "explanation": explanation,
        "code_preview": code_preview,
        "expanded_nl": expanded,
        "refs": refs,
    }
