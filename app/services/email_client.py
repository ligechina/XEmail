# Copyright (c) 2026 Peking University & Beijing Siliconheart Technology Co., Ltd.
# XEmail is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#          http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import email
import html
import imaplib
import logging
import re
import smtplib
import ssl
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

from app.services.rule_program import evaluate_program

logger = logging.getLogger(__name__)
from app.services.spam_filter import classify_via_llm

# The "no classification produced" sentinel. Kept here (rather than imported
# from app.storage to avoid a circular import) but must stay in sync with
# storage.UNCLASSIFIED_FOLDER.
UNCLASSIFIED = "未分类"


def _match_fixed_rule(rule: Dict, *, from_email: str, to_email: str,
                      cc_email: str, subject: str, body: str) -> bool:
    """Evaluate the rule's AST against the email. Returns False when the
    rule has no usable program (which keeps the receive pipeline moving on
    to the LLM step instead of crashing on a malformed record)."""
    program = rule.get("program")
    if not isinstance(program, dict) or not program:
        return False
    email = {
        "from": from_email or "",
        "to": to_email or "",
        "cc": cc_email or "",
        "subject": subject or "",
        "body": body or "",
    }
    try:
        return evaluate_program(program, email)
    except Exception:
        return False


def classify_email_record(
    *,
    from_email: str,
    to_email: str,
    subject: str,
    body: str,
    cc_email: str = "",
    attachments: Optional[List[str]] = None,
    fixed_rules: Optional[List[Dict]] = None,
    available_folders: Optional[List[str]] = None,
    system_prompt: Optional[str] = None,
    user_prompts_with_targets: Optional[List[Dict]] = None,
    field_config: Optional[Dict] = None,
) -> Tuple[str, bool, str]:
    """Per-email classification pipeline. Returns (category, important, reason).

    Order: fixed rules (first match wins) → LLM → 未分类 sentinel. Reusable
    by both the receive flow and the manual "智能分类" retry endpoint.

    `important` carries the LLM's verdict on whether the email should be
    flagged as 重要. Fixed rules never set it (they only route a folder);
    callers should treat False as "no opinion" when no LLM verdict was
    produced — see receive_emails / classify_unsorted / reclassify_all_stream.
    """
    # 1) Programmatic fixed rules. Rules with target "*" ("全部") are not
    # routing rules — they match across all folders so they can't pick a
    # destination. We let those fall through to the LLM step instead of
    # returning early; the rule's NL text is forwarded to the LLM as general
    # guidance (see _classification_context in main.py).
    for rule in fixed_rules or []:
        if _match_fixed_rule(
            rule,
            from_email=from_email,
            to_email=to_email,
            cc_email=cc_email,
            subject=subject,
            body=body,
        ):
            target = (rule.get("target_folder") or "").strip()
            if target and target != "*":
                nl_excerpt = (rule.get("nl_text") or "").strip().replace("\n", " ")
                if len(nl_excerpt) > 40:
                    nl_excerpt = nl_excerpt[:40] + "…"
                return target, False, f"固定规则命中: {nl_excerpt or rule.get('id')}"

    # 2) LLM (only if user prompts exist or system prompt is configured —
    # otherwise we'd waste a call doing nothing useful; but the LLM is also
    # what gives spam detection out of the box, so we always try when
    # available_folders is populated).
    cat, important, reason = classify_via_llm(
        from_email,
        subject,
        body,
        to_email=to_email,
        attachments=attachments,
        system_prompt=system_prompt,
        user_prompts_with_targets=user_prompts_with_targets,
        available_folders=available_folders,
        field_config=field_config,
    )
    if cat and (not available_folders or cat in available_folders):
        return cat, important, reason

    # 3) Tombstone for unsorted emails. Reason carries forward whatever
    # context we have (e.g. "no api key", "llm error: ...") to aid debugging.
    # Even on unclassified, an LLM-reported important=true survives — the
    # user told us to flag it regardless of which folder it ends up in.
    return UNCLASSIFIED, important, reason or "未命中固定规则且 LLM 无有效分类"


def _split_address_header(raw: str) -> List[str]:
    """Split a free-form address header ('Name <a@b>, c@d, …') into bare
    email addresses for SMTP RCPT TO. Display names are dropped. Empty /
    malformed entries are skipped.

    Falls back to a regex scan when getaddresses returns garbage — it
    chokes on display names that themselves contain '@' (a real header
    shape: `"foo@x.com" <foo@x.com>` or `foo@x.com <foo@x.com>`), returning
    `[('', '')]` for the entire input. Without the fallback those headers
    look empty to the server and the user gets "收件人不能为空"."""
    from email.utils import getaddresses

    if not raw:
        return []
    out: List[str] = []
    seen: set = set()
    for _name, addr in getaddresses([raw]):
        addr = (addr or "").strip()
        if addr and "@" in addr and addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    if out:
        return out
    # Regex fallback. Prefer addresses inside angle brackets; otherwise grab
    # bare `local@domain` tokens. We split on both commas and semicolons
    # because some Outlook flows use semicolons.
    addr_re = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>|([^\s<>,;]+@[^\s<>,;]+)")
    for piece in re.split(r"[,;]", raw):
        for m in addr_re.finditer(piece):
            cand = (m.group(1) or m.group(2) or "").strip().strip(".,;:'\"")
            if cand and "@" in cand and cand.lower() not in seen:
                seen.add(cand.lower())
                out.append(cand)
    return out


def send_email(
    settings: Dict,
    to: str,
    subject: str,
    body: str,
    attachments: List[Dict] = None,
    cc: str = "",
    bcc: str = "",
) -> bytes:
    """Build the message, send it over SMTP, and return the raw RFC822 bytes
    (useful for IMAP APPEND to a Sent folder).

    `to` and `cc` are written into the message headers, so recipients can
    see them. `bcc` is honored at the SMTP RCPT-TO layer ONLY — it never
    gets written into a header, so To/Cc recipients can't see Bcc'd
    addresses (standard mail-client semantics)."""
    msg = EmailMessage()
    msg["From"] = settings["sender_email"]
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg.set_content(body)

    for att in attachments or []:
        data = att.get("data")
        if data is None:
            continue
        ctype = att.get("content_type") or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            data,
            maintype=maintype,
            subtype=subtype,
            filename=att.get("filename") or "attachment",
        )

    # Build the dedup'd recipient list for SMTP. send_message() infers
    # to_addrs from the message headers when not supplied — if we let it
    # do that, Bcc would be skipped (we never put it in a header). So we
    # always pass to_addrs explicitly with To+Cc+Bcc merged.
    rcpt_seen: set = set()
    rcpts: List[str] = []
    for src in (to, cc, bcc):
        for addr in _split_address_header(src):
            key = addr.lower()
            if key in rcpt_seen:
                continue
            rcpt_seen.add(key)
            rcpts.append(addr)

    if settings.get("smtp_use_ssl", True):
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            settings["smtp_host"], settings["smtp_port"], context=context
        ) as server:
            server.login(settings["sender_email"], settings["sender_password"])
            server.send_message(
                msg,
                from_addr=settings["sender_email"],
                to_addrs=rcpts or None,
            )
        return bytes(msg)

    with smtplib.SMTP(settings["smtp_host"], settings["smtp_port"]) as server:
        if settings.get("smtp_use_starttls", False):
            server.starttls(context=ssl.create_default_context())
        server.login(settings["sender_email"], settings["sender_password"])
        server.send_message(
            msg,
            from_addr=settings["sender_email"],
            to_addrs=rcpts or None,
        )
    return bytes(msg)


def receive_emails(
    settings: Dict,
    days: int = 30,
    system_prompt: Optional[str] = None,
    user_prompts_with_targets: Optional[List[Dict]] = None,
    fixed_rules: Optional[List[Dict]] = None,
    available_folders: Optional[List[str]] = None,
    field_config: Optional[Dict] = None,
    on_progress=None,
    sync_state: Optional[Dict[str, Dict[str, str]]] = None,
    on_batch_ready: Optional[Any] = None,
    batch_size: int = 10,
    known_uids: Optional[set] = None,
) -> Tuple[List[Dict], Dict[str, str]]:
    """Fetch emails received in the last `days` days, deduplicated against
    what we already have. Returns `(records, sync_meta)` where `sync_meta`
    carries the mailbox name, UIDVALIDITY, and highest UID seen — the caller
    persists it so the next click only pulls truly new mail.

    Four layers conspire to prevent re-fetching the same email:
      1. Day window      — IMAP `SINCE <date>` filters to the user-chosen
         lookback window (1–100 days).
      2. UID watermark   — when we have a stored `{uidvalidity, last_uid}`
         for this mailbox AND UIDVALIDITY still matches, SEARCH is
         additionally narrowed to `UID <last_uid+1>:*`, so even within
         the day window we only see UIDs we've never processed. The
         watermark is dropped for one run when the user widens
         `fetch_days` (see `widened` below).
      3. Known-UID skip  — `known_uids`, if supplied by the caller, is
         the set of `imap_uid` strings already in the local store for
         this account. We drop those from the SEARCH result BEFORE the
         per-email FETCH loop so widened-mode fetches don't redownload
         + reclassify mail we already have. (Layer 2 covers the common
         case; layer 3 covers widened fetches where the UID filter is
         off.)
      4. Message-Id dedupe — the caller still runs `dedupe_by_message_id`
         as a belt-and-braces guard against UIDVALIDITY rolls or rare
         server quirks that would otherwise let a duplicate slip
         through.

    `on_progress`, when callable, is invoked with dict events as work
    proceeds — used by the streaming endpoint to push UI updates while a
    long fetch is still in flight.

    `on_batch_ready`, when callable, is invoked every `batch_size` emails
    (default 10) AND once at the end (with any leftover). Signature:
        on_batch_ready(records_in_batch: List[Dict],
                        sync_meta_so_far: Dict[str, str]) -> None
    The caller persists the batch + advances the watermark; on the next
    iteration we hand over a fresh empty list. This lets the UI see new
    mail in 10-email chunks rather than only at the very end.

    Event shapes:
      {"type": "connected"}                         after IMAP login + SELECT
      {"type": "planned",  "total": N,
        "mode": "incremental"|"initial"|"widened",
        "days": D,
        "already_known": K}                         after UID SEARCH; `total`
                                                    is the count of UIDs we
                                                    will actually process
                                                    (already-stored mail
                                                    already filtered out);
                                                    `already_known` reports
                                                    how many were skipped.
      {"type": "classified", "index": i, "total": N,
        "subject": ..., "from": ...,
        "received_at": "<ISO-8601 UTC>",
        "category": ..., "reason": ...}             after each per-email classify
      {"type": "skipped",  "index": i, "total": N, "uid": ..., "reason": ...}
                                                    on per-email failure
    """
    # Clamp to the same bounds as SyncSettings; defensive — the route layer
    # already validates, but a stray test or future caller might not.
    try:
        days_int = int(days)
    except (TypeError, ValueError):
        days_int = 30
    days_int = max(1, min(100, days_int))

    # IMAP date format per RFC 3501: `DD-Mon-YYYY` with English short month
    # name (locale-independent — don't use strftime("%b")).
    _MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days_int)
    since_date = f"{cutoff_dt.day:02d}-{_MON[cutoff_dt.month - 1]}-{cutoff_dt.year}"

    if settings.get("imap_use_ssl", True):
        mail = imaplib.IMAP4_SSL(settings["imap_host"], settings["imap_port"])
    else:
        mail = imaplib.IMAP4(settings["imap_host"], settings["imap_port"])

    def _notify(ev: Dict) -> None:
        if on_progress:
            try:
                on_progress(ev)
            except Exception:
                pass  # best-effort: progress must never break receiving

    try:
        mail.login(settings["sender_email"], settings["sender_password"])
        _send_imap_id(mail, settings)
        selected_mailbox = _select_inbox(mail)
        current_uidvalidity = _read_uidvalidity(mail)
        _notify({"type": "connected"})

        # Decide between incremental and initial fetch. Incremental requires:
        #   (a) we have a saved watermark for THIS mailbox,
        #   (b) UIDVALIDITY matches (UIDs weren't renumbered server-side),
        #   (c) we actually got a UIDVALIDITY back (else we can't trust it),
        #   (d) the user has NOT widened sync.fetch_days since the last
        #       successful fetch. Widening is detected via the stored
        #       `fetch_days_at`. If they widened (e.g. 14 → 60), the older
        #       mail in the newly-included gap has UIDs *lower* than
        #       last_uid, so a `UID <last+1>:*` query would never see it.
        #       We drop the UID filter for this run; message-id dedupe at
        #       the caller still prevents re-storing what we already have.
        # In all modes we additionally AND with `SINCE <cutoff>` so the day
        # window is always respected.
        stored = (sync_state or {}).get(selected_mailbox) or {}
        stored_uidvalidity = str(stored.get("uidvalidity") or "")
        stored_last_uid = str(stored.get("last_uid") or "")
        stored_fetch_days_at_raw = str(stored.get("fetch_days_at") or "")
        stored_fetch_days_at = (
            int(stored_fetch_days_at_raw) if stored_fetch_days_at_raw.isdigit() else 0
        )
        has_prior_watermark = (
            bool(current_uidvalidity)
            and bool(stored_uidvalidity)
            and current_uidvalidity == stored_uidvalidity
            and stored_last_uid.isdigit()
        )
        # "widened" fires when we cannot vouch that the prior watermark already
        # covers the requested day window. Two cases:
        #   1. stored_fetch_days_at is unknown (legacy state from before this
        #      field was introduced) — we have a watermark but no record of
        #      how wide a window it was built against. Safest assumption is
        #      "narrower than the user wants right now", so drop the UID
        #      filter for this run.
        #   2. user explicitly widened: days_int > stored_fetch_days_at.
        widened = has_prior_watermark and (
            stored_fetch_days_at <= 0 or days_int > stored_fetch_days_at
        )
        can_incremental = has_prior_watermark and not widened

        if can_incremental:
            since_uid = int(stored_last_uid) + 1
            criteria = f"UID {since_uid}:* SINCE {since_date}"
            mode = "incremental"
        else:
            criteria = f"SINCE {since_date}"
            # Surface "widened" distinctly from a first-time fetch so the UI
            # / logs can tell them apart; the underlying IMAP query is the
            # same.
            mode = "widened" if widened else "initial"
        status, data = mail.uid("SEARCH", None, criteria)
        if status != "OK":
            raise RuntimeError(
                f"IMAP UID SEARCH 失败: status={status}, data={_format_imap_data(data)}"
            )

        uids = (data[0] or b"").split()
        if can_incremental:
            # IMAP `N:*` semantics always include "*" — if no UIDs exist >= N,
            # the server still returns the highest existing UID. Filter so we
            # don't reprocess the watermark email itself.
            cutoff_uid = int(stored_last_uid)
            uids = [u for u in uids if u.isdigit() and int(u) > cutoff_uid]

        # Drop UIDs we've already stored locally. In incremental mode this is
        # a no-op (the UID lower bound already excluded them); in
        # widened/initial mode the SINCE-only SEARCH returns UIDs we already
        # have, and without this filter we'd redownload bodies + run the
        # LLM classifier on each one only to have dedupe_by_message_id throw
        # them away at the caller. Skipping them up-front saves bandwidth,
        # LLM quota, and makes the progress count meaningful (the user sees
        # 1/N..N/N where N is "new mail" rather than "everything in window").
        already_known_count = 0
        if known_uids:
            before = len(uids)
            uids = [
                u for u in uids
                if u and u.decode("ascii", errors="replace") not in known_uids
            ]
            already_known_count = before - len(uids)
        total = len(uids)
        _notify({
            "type": "planned",
            "total": total,
            "mode": mode,
            "days": days_int,
            "already_known": already_known_count,
        })
        records: List[Dict] = []
        # Accumulator for the next batch flush; same dict refs also live in
        # `records`, so popping `_pending_attachments` in the callback is
        # observed by the final return path too (idempotent).
        batch_pending: List[Dict] = []
        try:
            batch_size_int = max(1, int(batch_size))
        except (TypeError, ValueError):
            batch_size_int = 10

        def _flush_batch():
            """Hand the current batch + cumulative sync_meta to the callback.
            Cumulative max UID is computed from every successful record seen
            so far (not just the batch), so the watermark advances even when
            the LAST record of a batch is older than an earlier one."""
            if not on_batch_ready or not batch_pending:
                return
            seen = [
                int(r["imap_uid"])
                for r in records
                if str(r.get("imap_uid", "")).isdigit()
            ]
            # Never let the watermark regress: in widened/initial mode we
            # may see UIDs *below* the prior watermark (older mail), but the
            # max we should persist is still max(prior, new).
            candidates = list(seen)
            if stored_last_uid.isdigit():
                candidates.append(int(stored_last_uid))
            partial_last_uid = str(max(candidates)) if candidates else ""
            partial_meta = {
                "mailbox": selected_mailbox,
                "uidvalidity": str(current_uidvalidity or ""),
                "last_uid": partial_last_uid,
                "fetch_days_at": str(max(stored_fetch_days_at, days_int)),
            }
            try:
                on_batch_ready(list(batch_pending), partial_meta)
            except Exception as cb_exc:
                # Callback failure must not break the receive loop; log and
                # move on. The records stay in `records` so a non-callback
                # fallback path could still persist them.
                logger.warning("on_batch_ready failed: %s", cb_exc)
            finally:
                batch_pending.clear()

        for idx, uid_bytes in enumerate(uids):
            uid_str = uid_bytes.decode("ascii", errors="replace") if uid_bytes else ""
            # A single malformed email or transient classifier hiccup must not
            # nuke a 100+ email batch (each iteration may include an LLM call,
            # so the sunk cost is real). Anything raised in here is logged,
            # surfaced to the UI as a 'skipped' progress event, and we move on
            # to the next UID.
            try:
                # Use BODY.PEEK[] + INTERNALDATE instead of RFC822 so fetching
                # does NOT silently
                # set \Seen on the server — receiving must never change server
                # state, especially when the user has sync turned off.
                # INTERNALDATE is the server-side receive timestamp.
                status, msg_data = mail.uid(
                    "FETCH", uid_bytes, "(BODY.PEEK[] INTERNALDATE)"
                )
                if status != "OK" or not msg_data or msg_data[0] is None:
                    continue

                # imaplib FETCH returns either a tuple (envelope_bytes, body_bytes)
                # or a bare bytes literal (when the server only sends an envelope
                # for that UID — happens with deleted/expunged-mid-flight mail).
                # Skip silently in that case instead of crashing with IndexError.
                head = msg_data[0]
                if not isinstance(head, (tuple, list)) or len(head) < 2:
                    _notify(
                        {
                            "type": "skipped",
                            "index": idx + 1,
                            "total": total,
                            "uid": uid_str,
                            "reason": "服务器未返回邮件正文（可能已被删除）",
                        }
                    )
                    continue

                raw_bytes = head[1]
                if not raw_bytes:
                    continue

                parsed = email.message_from_bytes(raw_bytes, policy=policy.default)
                subject = str(parsed.get("Subject", "") or "")
                from_raw = _decode_header(str(parsed.get("From", "") or "")).strip()
                to_raw = _decode_header(str(parsed.get("To", "") or "")).strip()
                cc_raw = _decode_header(str(parsed.get("Cc", "") or "")).strip()
                from_email = parseaddr(from_raw)[1]
                to_email = parseaddr(to_raw)[1]
                # Cc kept as the full decoded header text — rules can do
                # contains/equals/regex against it; the UI parses it to render
                # individual recipient chips in the detail pane.
                cc_email = cc_raw
                message_id = str(parsed.get("Message-ID") or "") or str(uuid.uuid4())
                message_ids_in_refs = _extract_message_ids(
                    str(parsed.get("References", "") or "")
                )
                in_reply_to = _extract_first_message_id(
                    str(parsed.get("In-Reply-To", "") or "")
                )
                source_message_id = (
                    (message_ids_in_refs[0] if message_ids_in_refs else "")
                    or in_reply_to
                    or message_id
                )
                body, body_html = _extract_body(parsed)
                server_received_at = _resolve_server_received_at(
                    parsed=parsed,
                    fetch_envelope=head[0] if isinstance(head, (tuple, list)) else None,
                )
                try:
                    pending_attachments = _extract_attachments(parsed)
                except Exception:
                    pending_attachments = []
                attach_names = [
                    (item[0] if isinstance(item, (tuple, list)) and item else "")
                    for item in (pending_attachments or [])
                ]
                # Two-stage classification: programmatic fixed rules first, then
                # the LLM. Anything still unsorted gets the 未分类 tombstone so
                # the user can retry with the «智能分类» button later.
                category, important, classification_reason = classify_email_record(
                    from_email=from_email,
                    to_email=to_email,
                    cc_email=cc_email,
                    subject=subject,
                    body=body,
                    attachments=attach_names,
                    fixed_rules=fixed_rules,
                    available_folders=available_folders,
                    system_prompt=system_prompt,
                    user_prompts_with_targets=user_prompts_with_targets,
                    field_config=field_config,
                )

                new_record = {
                    "id": str(uuid.uuid4()),
                    "message_id": message_id,
                    "from_email": from_email,
                    "to_email": to_email or settings["receiver_email"],
                    "cc_email": cc_email,
                    "from_raw": from_raw,
                    "to_raw": to_raw,
                    "cc_raw": cc_raw,
                    "subject": subject,
                    "body": body,
                    "body_html": body_html,
                    "received_at": server_received_at,
                    "in_reply_to": in_reply_to or None,
                    "references": message_ids_in_refs,
                    "source_message_id": source_message_id or message_id,
                    "category": category,
                    "important": bool(important),
                    "attachments": [],
                    "imap_uid": uid_str,
                    "imap_mailbox": selected_mailbox,
                    "spam_reason": classification_reason,
                    "_pending_attachments": pending_attachments,
                }
                records.append(new_record)
                batch_pending.append(new_record)

                _notify(
                    {
                        "type": "classified",
                        "index": idx + 1,
                        "total": total,
                        "subject": subject,
                        "from": from_email,
                        "received_at": server_received_at,
                        "category": category,
                        "reason": classification_reason,
                    }
                )

                # Hand the chunk off to the caller so the user sees mail
                # showing up every batch_size emails instead of only at the
                # very end. The same dict references stay in `records`, so a
                # final return is still consistent.
                if len(batch_pending) >= batch_size_int:
                    _flush_batch()
            except Exception as per_email_exc:
                # Log the full traceback to the server log so the next time
                # this happens you can `grep` it out of /tmp/xemail-server.log.
                logger.warning(
                    "skipping UID %s (idx %d/%d): %s\n%s",
                    uid_str,
                    idx + 1,
                    total,
                    per_email_exc,
                    traceback.format_exc(),
                )
                _notify(
                    {
                        "type": "skipped",
                        "index": idx + 1,
                        "total": total,
                        "uid": uid_str,
                        "reason": f"{type(per_email_exc).__name__}: {per_email_exc}",
                    }
                )
                continue

        # Flush whatever's left in the last (partial) batch.
        _flush_batch()

        # New watermark = max(previous watermark, highest UID we just
        # processed). Including the previous value guards against regression
        # in widened-mode runs that fetched only mail older than the existing
        # watermark — there the highest UID *we saw* is below the watermark,
        # but the watermark still validly covers everything up to the prior
        # last_uid.
        seen_uids = [int(r["imap_uid"]) for r in records if str(r.get("imap_uid", "")).isdigit()]
        candidates = list(seen_uids)
        if stored_last_uid.isdigit():
            candidates.append(int(stored_last_uid))
        new_last_uid = str(max(candidates)) if candidates else ""

        sync_meta = {
            "mailbox": selected_mailbox,
            "uidvalidity": str(current_uidvalidity or ""),
            "last_uid": new_last_uid,
            # Widest window we've now covered end-to-end. If the user widens
            # again later we'll detect it; if they shrink, the wider value
            # still stands (the watermark already covers the wider range).
            "fetch_days_at": str(max(stored_fetch_days_at, days_int)),
        }
        return records, sync_meta
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def _read_uidvalidity(mail: imaplib.IMAP4) -> str:
    """Pull UIDVALIDITY from the most recent SELECT's untagged response.
    Returns "" when the server didn't advertise one — callers treat empty as
    "incremental fetch unsafe, do a full search instead"."""
    raw = mail.untagged_responses.get("UIDVALIDITY")
    if not raw:
        return ""
    first = raw[0]
    if isinstance(first, bytes):
        first = first.decode("ascii", errors="replace")
    return str(first).strip()


def dedupe_by_message_id(old_items: List[Dict], new_items: List[Dict]) -> Tuple[List[Dict], int]:
    merged = list(old_items)
    index_by_id: Dict[str, int] = {}
    for idx, item in enumerate(merged):
        msg_id = item.get("message_id")
        if msg_id:
            index_by_id[msg_id] = idx

    added = 0

    for item in new_items:
        msg_id = item.get("message_id")
        if msg_id and msg_id in index_by_id:
            idx = index_by_id[msg_id]
            existing = merged[idx]
            existing_empty = not (
                existing.get("body")
                or existing.get("body_html")
                or existing.get("attachments")
            )
            new_has_content = bool(
                item.get("body")
                or item.get("body_html")
                or item.get("_pending_attachments")
            )
            if existing_empty and new_has_content:
                # Upgrade the stored record while keeping its original id.
                # Caller is responsible for materialising _pending_attachments
                # under the resulting id after dedupe.
                upgraded = {**existing, **item, "id": existing.get("id") or item.get("id")}
                merged[idx] = upgraded
            continue
        merged.append(item)
        if msg_id:
            index_by_id[msg_id] = len(merged) - 1
        added += 1

    return merged, added


def _resolve_server_received_at(*, parsed, fetch_envelope: Any) -> str:
    """Pick the best available receive timestamp for storage/display.

    Priority:
      1) IMAP INTERNALDATE (server receive time)
      2) Message Date header
      3) Local now fallback

    Always normalized to UTC ISO-8601 for stable sorting in the UI.
    """
    from_internal = _extract_internaldate_iso_utc(fetch_envelope)
    if from_internal:
        return from_internal

    from_date = _date_header_to_iso_utc(str(parsed.get("Date", "") or ""))
    if from_date:
        return from_date

    return datetime.now(timezone.utc).isoformat()


def _extract_internaldate_iso_utc(fetch_envelope: Any) -> str:
    """Extract INTERNALDATE from FETCH envelope and convert to UTC ISO."""
    if isinstance(fetch_envelope, bytes):
        text = fetch_envelope.decode("utf-8", errors="replace")
    else:
        text = str(fetch_envelope or "")
    m = re.search(r'INTERNALDATE "([^"]+)"', text)
    if not m:
        return ""
    raw = m.group(1).strip()
    try:
        dt = datetime.strptime(raw, "%d-%b-%Y %H:%M:%S %z")
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def _date_header_to_iso_utc(date_header: str) -> str:
    """Parse RFC2822 Date header into UTC ISO-8601."""
    text = str(date_header or "").strip()
    if not text:
        return ""
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def _select_mailbox_by_name(mail: imaplib.IMAP4, mailbox: str) -> bool:
    """Best-effort mailbox select for a concrete mailbox name."""
    mb = (mailbox or "").strip()
    if not mb:
        return False
    for candidate in (mb, f'"{mb}"'):
        for readonly in (True, False):
            try:
                status, _ = mail.select(candidate, readonly=readonly)
                if status == "OK":
                    return True
            except Exception:
                continue
    return False


def repair_email_received_times(
    *,
    settings: Dict,
    records: List[Dict],
    on_progress=None,
) -> Dict[str, int]:
    """Backfill existing records' received_at from the mail server.

    For each record in `records` (typically one active account's inbox),
    attempts to fetch:
      - IMAP INTERNALDATE (preferred)
      - Date header (fallback)
    and updates `record["received_at"]` in-place when a better timestamp
    is available.
    """

    def _notify(ev: Dict) -> None:
        if on_progress:
            try:
                on_progress(ev)
            except Exception:
                pass

    total = len(records)
    updated = 0
    unchanged = 0
    skipped = 0
    scanned = 0

    if settings.get("imap_use_ssl", True):
        mail = imaplib.IMAP4_SSL(settings["imap_host"], settings["imap_port"])
    else:
        mail = imaplib.IMAP4(settings["imap_host"], settings["imap_port"])

    try:
        mail.login(settings["sender_email"], settings["sender_password"])
        _send_imap_id(mail, settings)
        selected_mailbox = _select_inbox(mail)
        _notify({"type": "connected"})
        _notify({"type": "planned", "total": total})

        for idx, rec in enumerate(records):
            uid = str(rec.get("imap_uid") or "").strip()
            mailbox = str(rec.get("imap_mailbox") or "").strip()
            if not uid.isdigit():
                skipped += 1
                _notify(
                    {
                        "type": "skipped",
                        "index": idx + 1,
                        "total": total,
                        "uid": uid,
                        "reason": "缺少 IMAP UID，无法回填服务器时间",
                    }
                )
                continue

            if mailbox and mailbox != selected_mailbox:
                if _select_mailbox_by_name(mail, mailbox):
                    selected_mailbox = mailbox
                elif _select_mailbox_by_name(mail, selected_mailbox):
                    pass
                else:
                    try:
                        selected_mailbox = _select_inbox(mail)
                    except Exception:
                        pass

            try:
                status, msg_data = mail.uid(
                    "FETCH",
                    uid.encode("ascii"),
                    "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (DATE MESSAGE-ID IN-REPLY-TO REFERENCES SUBJECT)])",
                )
                if status != "OK" or not msg_data or msg_data[0] is None:
                    skipped += 1
                    _notify(
                        {
                            "type": "skipped",
                            "index": idx + 1,
                            "total": total,
                            "uid": uid,
                            "reason": "服务器未返回可用时间信息",
                        }
                    )
                    continue

                head = msg_data[0]
                if not isinstance(head, (tuple, list)) or len(head) < 2:
                    skipped += 1
                    _notify(
                        {
                            "type": "skipped",
                            "index": idx + 1,
                            "total": total,
                            "uid": uid,
                            "reason": "FETCH 返回结构异常",
                        }
                    )
                    continue

                envelope = head[0]
                header_bytes = head[1] if isinstance(head[1], (bytes, bytearray)) else b""
                new_time = _extract_internaldate_iso_utc(envelope)
                if not new_time and header_bytes:
                    hdr = email.message_from_bytes(header_bytes, policy=policy.default)
                    new_time = _date_header_to_iso_utc(str(hdr.get("Date", "") or ""))
                    # Also backfill thread metadata so historical mail can
                    # participate in the "同源邮件合并" view.
                    repaired_mid = _extract_first_message_id(str(hdr.get("Message-ID", "") or ""))
                    repaired_refs = _extract_message_ids(str(hdr.get("References", "") or ""))
                    repaired_in_reply = _extract_first_message_id(
                        str(hdr.get("In-Reply-To", "") or "")
                    )
                    repaired_source = (
                        (repaired_refs[0] if repaired_refs else "")
                        or repaired_in_reply
                        or repaired_mid
                    )
                    if repaired_mid and not str(rec.get("message_id") or "").strip():
                        rec["message_id"] = repaired_mid
                    if repaired_in_reply:
                        rec["in_reply_to"] = repaired_in_reply
                    if repaired_refs:
                        rec["references"] = repaired_refs
                    if repaired_source:
                        rec["source_message_id"] = repaired_source
                    repaired_subject = _decode_header(str(hdr.get("Subject", "") or "")).strip()
                    if repaired_subject and not str(rec.get("subject") or "").strip():
                        rec["subject"] = repaired_subject
                if not new_time:
                    skipped += 1
                    _notify(
                        {
                            "type": "skipped",
                            "index": idx + 1,
                            "total": total,
                            "uid": uid,
                            "reason": "INTERNALDATE/Date 均不可用",
                        }
                    )
                    continue

                scanned += 1
                old_time = str(rec.get("received_at") or "")
                changed = old_time != new_time
                if changed:
                    rec["received_at"] = new_time
                    updated += 1
                else:
                    unchanged += 1
                _notify(
                    {
                        "type": "repaired",
                        "index": idx + 1,
                        "total": total,
                        "uid": uid,
                        "subject": rec.get("subject") or "",
                        "changed": changed,
                    }
                )
            except Exception as per_email_exc:
                skipped += 1
                _notify(
                    {
                        "type": "skipped",
                        "index": idx + 1,
                        "total": total,
                        "uid": uid,
                        "reason": f"{type(per_email_exc).__name__}: {per_email_exc}",
                    }
                )

        return {
            "total": total,
            "scanned": scanned,
            "updated": updated,
            "unchanged": unchanged,
            "skipped": skipped,
        }
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def diagnose_email_connection(settings: Dict) -> Dict:
    report = {
        "smtp": {"ok": False, "detail": ""},
        "imap": {"ok": False, "detail": ""},
    }

    # SMTP diagnose: connect -> optional STARTTLS -> login
    try:
        if settings.get("smtp_use_ssl", True):
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                settings["smtp_host"], settings["smtp_port"], context=context
            ) as server:
                server.login(settings["sender_email"], settings["sender_password"])
        else:
            with smtplib.SMTP(settings["smtp_host"], settings["smtp_port"]) as server:
                if settings.get("smtp_use_starttls", False):
                    server.starttls(context=ssl.create_default_context())
                server.login(settings["sender_email"], settings["sender_password"])
        report["smtp"] = {"ok": True, "detail": "SMTP 连接与登录成功。"}
    except Exception as exc:
        report["smtp"] = {"ok": False, "detail": str(exc)}

    # IMAP diagnose: connect -> login -> select inbox
    mail = None
    try:
        if settings.get("imap_use_ssl", True):
            mail = imaplib.IMAP4_SSL(settings["imap_host"], settings["imap_port"])
        else:
            mail = imaplib.IMAP4(settings["imap_host"], settings["imap_port"])
        mail.login(settings["sender_email"], settings["sender_password"])
        _send_imap_id(mail, settings)
        _select_inbox(mail)
        report["imap"] = {"ok": True, "detail": "IMAP 连接、登录与收件箱选择成功。"}
    except Exception as exc:
        report["imap"] = {"ok": False, "detail": str(exc)}
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

    return report


def _decode_header(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_message_ids(raw: str) -> List[str]:
    """Return all RFC822 msg-ids found in a header string."""
    text = str(raw or "")
    out: List[str] = []
    seen = set()
    for m in re.finditer(r"<[^<>]+>", text):
        mid = m.group(0).strip()
        if not mid:
            continue
        key = mid.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(mid)
    return out


def _extract_first_message_id(raw: str) -> str:
    ids = _extract_message_ids(raw)
    return ids[0] if ids else ""


def _extract_body(parsed_msg) -> Tuple[str, str]:
    plain_text = ""
    html_text = ""

    # Prefer the modern policy.default API: get_body() correctly descends into
    # nested multipart/alternative & multipart/related (Apple/marketing emails).
    html_text = _coerce_body(_safe_get_body(parsed_msg, ("html",)))
    plain_text = _coerce_body(_safe_get_body(parsed_msg, ("plain",)))

    # Fallback: walk every part. Some servers/messages don't play nicely with
    # get_body() (signed mail, malformed structure, legacy policy).
    if not plain_text or not html_text:
        for part in parsed_msg.walk():
            if part.is_multipart():
                continue
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue

            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue

            decoded = _decode_part(part)
            if not decoded:
                continue
            if content_type == "text/plain" and not plain_text:
                plain_text = decoded
            elif content_type == "text/html" and not html_text:
                html_text = decoded

    if not plain_text and html_text:
        plain_text = _html_to_text(html_text)

    return plain_text, html_text


def _extract_attachments(parsed_msg) -> List[Tuple[str, bytes, str]]:
    """Return [(filename, raw_bytes, content_type), ...] for all attachment-like
    parts on the message — anything with a filename or explicit attachment
    Content-Disposition. Inline body text/* parts are excluded."""
    out: List[Tuple[str, bytes, str]] = []
    for part in parsed_msg.walk():
        if part.is_multipart():
            continue
        disposition = str(part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if not filename and "attachment" not in disposition:
            continue
        if filename:
            try:
                filename = str(make_header(decode_header(filename)))
            except Exception:
                filename = str(filename)
        else:
            filename = "attachment"
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        ctype = part.get_content_type() or "application/octet-stream"
        out.append((filename, payload, ctype))
    return out


def _safe_get_body(parsed_msg, preferencelist):
    getter = getattr(parsed_msg, "get_body", None)
    if not callable(getter):
        return None
    try:
        return getter(preferencelist=preferencelist)
    except Exception:
        return None


def _coerce_body(part) -> str:
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, KeyError, Exception):
        content = None

    if content is None:
        content = _decode_part(part)

    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if not isinstance(content, str):
        return ""
    return content.strip()


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace").strip()
    except (LookupError, AttributeError):
        return payload.decode("utf-8", errors="replace").strip()


def _select_inbox(mail: imaplib.IMAP4) -> str:
    list_status, mailboxes = mail.list()
    parsed_mailboxes = _parse_mailbox_names(mailboxes) if list_status == "OK" else []

    mailbox_candidates = ["INBOX", "Inbox", "inbox"]
    for name in parsed_mailboxes:
        if name.upper() == "INBOX" and name not in mailbox_candidates:
            mailbox_candidates.append(name)

    errors: List[str] = []
    for mailbox in mailbox_candidates:
        for readonly in (False, True):
            status, data = mail.select(mailbox, readonly=readonly)
            if status == "OK":
                return mailbox
            mode = "readonly" if readonly else "readwrite"
            errors.append(
                f"{mailbox}({mode}) => status={status}, data={_format_imap_data(data)}"
            )

    available_mailboxes = _format_imap_data(mailboxes) if list_status == "OK" else "未知"
    raise RuntimeError(
        "无法选择收件箱（INBOX）。请确认邮箱已开启 IMAP，"
        f"并检查账号权限。可见邮箱列表: {available_mailboxes}。"
        f"尝试详情: {' || '.join(errors) if errors else '无'}"
    )


def _format_imap_data(data) -> str:
    if not data:
        return "[]"
    chunks = []
    for item in data:
        if isinstance(item, bytes):
            chunks.append(item.decode(errors="replace"))
        else:
            chunks.append(str(item))
    return " | ".join(chunks)


def _parse_mailbox_names(mailboxes) -> List[str]:
    if not mailboxes:
        return []

    names: List[str] = []
    for raw in mailboxes:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)

        # Typical IMAP LIST response: () "/" "INBOX"
        match = re.search(r'"((?:[^"\\]|\\.)*)"\s*$', line)
        if match:
            names.append(match.group(1).replace('\\"', '"'))
            continue

        # Fallback for uncommon non-quoted mailbox names.
        parts = line.rsplit(" ", 1)
        if len(parts) == 2:
            names.append(parts[-1].strip())

    return [name for name in names if name]


def _send_imap_id(mail: imaplib.IMAP4, settings: Dict) -> None:
    if not settings.get("imap_send_id", True):
        return

    id_info = {
        "name": str(settings.get("imap_id_name", "XEmail")),
        "version": str(settings.get("imap_id_version", "0.1.0")),
        "vendor": str(settings.get("imap_id_vendor", "XEmail")),
        "support-email": str(
            settings.get("imap_id_support_email", "support@example.com")
        ),
    }
    payload = _build_imap_id_payload(id_info)
    # Python's imaplib may not pre-register RFC2971 ID in module Commands.
    if "ID" not in imaplib.Commands:
        imaplib.Commands["ID"] = ("AUTH", "SELECTED")

    try:
        status, data = mail._simple_command("ID", payload)
    except KeyError:
        # Defensive retry for environments with stale command mapping.
        if "ID" not in imaplib.Commands:
            imaplib.Commands["ID"] = ("AUTH", "SELECTED")
        try:
            status, data = mail._simple_command("ID", payload)
        except Exception as inner_exc:
            raise RuntimeError(f"IMAP ID 失败: {inner_exc}") from inner_exc
    except Exception as exc:
        raise RuntimeError(f"IMAP ID 失败: {exc}") from exc

    if status == "OK":
        return

    detail = _format_imap_data(data)
    lower = detail.lower()
    # Some servers don't support RFC2971 ID. Ignore this incompatibility.
    if status == "BAD" and ("unknown" in lower or "unsupported" in lower):
        return

    raise RuntimeError(f"IMAP ID 失败: status={status}, data={detail}")


def _build_imap_id_payload(id_info: Dict[str, str]) -> str:
    chunks = []
    for key, value in id_info.items():
        k = _escape_imap_quoted(key)
        v = _escape_imap_quoted(value)
        chunks.append(f'"{k}" "{v}"')
    return f"({' '.join(chunks)})"


def _escape_imap_quoted(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _html_to_text(html_content: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_content)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ----------------- IMAP sync helpers -----------------


def _imap_connect(settings: Dict) -> imaplib.IMAP4:
    if settings.get("imap_use_ssl", True):
        mail = imaplib.IMAP4_SSL(settings["imap_host"], settings["imap_port"])
    else:
        mail = imaplib.IMAP4(settings["imap_host"], settings["imap_port"])
    mail.login(settings["sender_email"], settings["sender_password"])
    _send_imap_id(mail, settings)
    return mail


def imap_set_flags(
    settings: Dict,
    mailbox: str,
    uid: str,
    add: List[str] = None,
    remove: List[str] = None,
) -> None:
    """STORE +/-FLAGS on a UID in a specific mailbox. Raises on failure."""
    if not uid:
        return
    mail = _imap_connect(settings)
    try:
        status, _ = mail.select(mailbox or "INBOX")
        if status != "OK":
            raise RuntimeError(f"无法选择 mailbox {mailbox}")
        if add:
            mail.uid("STORE", uid, "+FLAGS", f"({' '.join(add)})")
        if remove:
            mail.uid("STORE", uid, "-FLAGS", f"({' '.join(remove)})")
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def imap_expunge_uid(settings: Dict, mailbox: str, uid: str) -> None:
    """Set \\Deleted and EXPUNGE the message."""
    if not uid:
        return
    mail = _imap_connect(settings)
    try:
        status, _ = mail.select(mailbox or "INBOX")
        if status != "OK":
            raise RuntimeError(f"无法选择 mailbox {mailbox}")
        mail.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        mail.expunge()
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def imap_append_sent(settings: Dict, raw_message: bytes) -> None:
    """Append the raw RFC822 bytes to the server's Sent folder (best effort).
    Tries common Sent mailbox names — many providers will auto-route SMTP-sent
    messages to Sent on their own, so this is purely a safety net."""
    mail = _imap_connect(settings)
    try:
        # Find a Sent-like mailbox.
        list_status, mailboxes = mail.list()
        names = _parse_mailbox_names(mailboxes) if list_status == "OK" else []
        candidates = [
            "Sent",
            "Sent Messages",
            "Sent Items",
            "已发送",
            "已发邮件",
            "&XfJT0ZAB-",  # IMAP UTF-7 for "已发送" used by some 163 servers
        ]
        # Prefer ones that actually exist on the server.
        target = next((n for n in candidates if n in names), None)
        if target is None:
            # Try matching case-insensitively by suffix.
            for n in names:
                if n.lower().endswith("sent") or "sent" in n.lower():
                    target = n
                    break
        if target is None:
            return  # silently give up — most servers handle this automatically
        mail.append(target, r"(\Seen)", imaplib.Time2Internaldate(time.time()), raw_message)
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def imap_move_uid(settings: Dict, src_mailbox: str, uid: str, dst_path: str) -> None:
    """Move a message to another server-side mailbox path. Creates destination if
    missing. If hierarchical paths aren't supported, falls back to the parent."""
    if not uid:
        return
    mail = _imap_connect(settings)
    try:
        status, _ = mail.select(src_mailbox or "INBOX")
        if status != "OK":
            raise RuntimeError(f"无法选择 mailbox {src_mailbox}")
        # Probe server delimiter from LIST.
        list_status, mailboxes = mail.list()
        delimiter = "/"
        if list_status == "OK" and mailboxes:
            first = (mailboxes[0] or b"").decode(errors="replace") if isinstance(mailboxes[0], bytes) else str(mailboxes[0])
            m = re.search(r'\(([^)]*)\) "([^"]+)" ', first)
            if m:
                delimiter = m.group(2) or "/"

        server_path = dst_path.replace("/", delimiter)
        names = _parse_mailbox_names(mailboxes) if list_status == "OK" else []

        # If full hierarchical path can't be created, try fallback to parent.
        path_to_try = server_path
        if path_to_try not in names:
            # Try creating progressively.
            parts = server_path.split(delimiter)
            created = False
            for i in range(1, len(parts) + 1):
                attempt = delimiter.join(parts[:i])
                if attempt in names:
                    continue
                status, _ = mail.create(attempt)
                if status != "OK":
                    # Hierarchy not supported here; fall back to deepest existing
                    # ancestor or the root segment.
                    path_to_try = delimiter.join(parts[: max(i - 1, 1)])
                    break
                created = True
            if created and path_to_try == server_path:
                pass  # created the requested path
            elif path_to_try not in names and path_to_try != server_path:
                # No usable target found; leave message in place.
                return

        # Prefer MOVE (RFC 6851); fall back to COPY + STORE \Deleted + EXPUNGE.
        try:
            mail.uid("MOVE", uid, path_to_try)
        except Exception:
            mail.uid("COPY", uid, path_to_try)
            mail.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            mail.expunge()
    finally:
        try:
            mail.logout()
        except Exception:
            pass
