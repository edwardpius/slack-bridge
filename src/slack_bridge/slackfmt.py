"""Markdown report -> Slack (REQ-15, REQ-16).

Slack's mrkdwn differs from Markdown, and a section block holds at most 3,000 characters.
"""
from __future__ import annotations

import re

SECTION_LIMIT = 2900          # under Slack's 3,000 per section block
SUMMARY_LIMIT = 2800

_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.M)
_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_DOWNLOAD = re.compile(r"\[Download the report\]\(([^)\s]+)\)")


def absolutize(md: str, base_url: str) -> str:
    """Relative hub links (`/reports/...`) become absolute on the kagent host."""
    base = base_url.rstrip("/")
    return re.sub(r"\]\((/[^)\s]*)\)", lambda m: f"]({base}{m.group(1)})", md)


def download_links(md: str) -> list[str]:
    return _DOWNLOAD.findall(md)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_mrkdwn(md: str) -> str:
    """Markdown -> Slack mrkdwn, leaving fenced code alone."""
    out = []
    for i, chunk in enumerate(re.split(r"(```.*?```)", md, flags=re.S)):
        if i % 2:
            out.append(chunk)
            continue
        links: list[str] = []

        def keep_link(m: re.Match) -> str:
            url = m.group(2).replace(">", "%3E").replace("|", "%7C")
            links.append(f"<{url}|{_escape(m.group(1))}>")
            return f"\x00{len(links) - 1}\x00"

        s = _LINK.sub(keep_link, chunk)
        s = _escape(s)
        s = _ITALIC.sub(r"_\1_", s)
        s = _BOLD.sub(lambda m: f"*{m.group(1) or m.group(2)}*", s)
        s = _HEADING.sub(r"*\1*", s)
        s = _BULLET.sub(r"\1• ", s)
        s = re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], s)
        out.append(s)
    return "".join(out)


def split(text: str, limit: int = SECTION_LIMIT) -> list[str]:
    """Pieces under `limit`, cut at paragraph, then line, then hard boundaries."""
    pieces: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        cand = f"{cur}\n\n{para}" if cur else para
        if len(cand) <= limit:
            cur = cand
            continue
        if cur:
            pieces.append(cur)
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(para[:cut])
            para = para[cut:].lstrip("\n")
        cur = para
    if cur:
        pieces.append(cur)
    return pieces


def summary(md: str, limit: int = SUMMARY_LIMIT) -> tuple[str, bool]:
    """The report up to its second section (title and summary), clipped. (text, clipped?)"""
    body = md.split("\n---\n", 1)[0]                 # drop research-agent's stats footer
    m = list(re.finditer(r"^#{1,3}\s", body, re.M))
    head = body[: m[2].start()] if len(m) > 2 else body
    head = _DOWNLOAD.sub("", head).strip()
    clipped = len(head) < len(body.strip())
    if len(head) > limit:
        head = split(head, limit)[0]
        clipped = True
    return head, clipped


def footer(md: str) -> str:
    """research-agent's stats line (turns, searches, cost, time), if present."""
    parts = md.rsplit("\n---\n", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def blocks_for(text_mrkdwn: str) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": p}}
            for p in split(text_mrkdwn)][:45]
