"""Markdown conversion utilities for RELAIS outgoing channels.

Converts standard Markdown to the platform-specific dialects required
by Telegram (MarkdownV2), Slack (mrkdwn), and plain-text output.

No external dependencies — stdlib ``re`` only.
"""
import re

# ---------------------------------------------------------------------------
# Telegram MarkdownV2
# ---------------------------------------------------------------------------

# Characters that must be escaped in all MarkdownV2 text contexts
# (outside code spans / pre blocks), per Telegram Bot API docs.
_TELEGRAM_ESCAPE_CHARS = r"_*[]()~`>#+-=|{}.!"

# Compiled regex: matches any character from the escape set
_TELEGRAM_ESCAPE_RE = re.compile(r"([" + re.escape(_TELEGRAM_ESCAPE_CHARS) + r"])")


def _escape_telegram(text: str) -> str:
    """Escapes all Telegram MarkdownV2 special characters in plain text.

    Args:
        text: Raw text fragment that should appear as-is in Telegram output.

    Returns:
        Text with all MarkdownV2 special characters backslash-escaped.
    """
    return _TELEGRAM_ESCAPE_RE.sub(r"\\\1", text)


def convert_md_to_telegram(text: str) -> str:
    """Converts standard Markdown to Telegram MarkdownV2 format.

    Handles: bold (**), italic (*/_), inline code (`), code blocks (```),
    links ([text](url)), strikethrough (~~), and escapes all remaining
    special characters in plain-text spans.

    Args:
        text: Input text in standard Markdown format.

    Returns:
        Text formatted as Telegram MarkdownV2.
    """
    result: list[str] = []
    # Process fenced code blocks first (preserve content verbatim inside)
    segments = re.split(r"(```(?:[^\n]*\n)?[\s\S]*?```)", text)
    for segment in segments:
        if segment.startswith("```"):
            # Extract language hint and body
            inner = re.sub(r"^```[^\n]*\n?", "", segment)
            inner = re.sub(r"```$", "", inner)
            result.append(f"```{inner}```")
            continue

        # Inline code — escape content inside backticks per TG spec
        def _inline_code(m: re.Match) -> str:
            return f"`{m.group(1)}`"

        segment = re.sub(r"`([^`]+)`", _inline_code, segment)

        # Bold — **text** → *text*  (MarkdownV2 uses single * for bold)
        # Null-byte placeholders protect bold output from the italic pass below,
        # which would otherwise convert the freshly-emitted *text* to _text_.
        _BOLD_OPEN = "\x00BO\x00"
        _BOLD_CLOSE = "\x00BC\x00"
        segment = re.sub(
            r"\*\*(.+?)\*\*",
            lambda m: f"{_BOLD_OPEN}{_escape_telegram(m.group(1))}{_BOLD_CLOSE}",
            segment,
        )
        # Bold alternative __text__
        segment = re.sub(
            r"__(.+?)__",
            lambda m: f"{_BOLD_OPEN}{_escape_telegram(m.group(1))}{_BOLD_CLOSE}",
            segment,
        )
        # Italic — *text* or _text_ → _text_  (bold placeholders are not matched)
        segment = re.sub(r"\*([^*]+)\*", lambda m: f"_{_escape_telegram(m.group(1))}_", segment)
        segment = re.sub(r"_([^_]+)_", lambda m: f"_{_escape_telegram(m.group(1))}_", segment)
        # Restore bold placeholders → MarkdownV2 *
        segment = segment.replace(_BOLD_OPEN, "*").replace(_BOLD_CLOSE, "*")
        # Strikethrough — ~~text~~ → ~text~
        segment = re.sub(r"~~(.+?)~~", lambda m: f"~{_escape_telegram(m.group(1))}~", segment)
        # Links — [text](url) → [text](url)  (URL must not be escaped)
        segment = re.sub(
            r"\[([^\]]+)\]\(([^)]+)\)",
            lambda m: f"[{_escape_telegram(m.group(1))}]({m.group(2)})",
            segment,
        )
        # Headings — strip # prefix, keep text bold
        segment = re.sub(
            r"^#{1,6}\s+(.+)$",
            lambda m: f"*{_escape_telegram(m.group(1))}*",
            segment,
            flags=re.MULTILINE,
        )
        # Escape remaining plain-text portions only — avoid double-escaping the
        # MarkdownV2 formatting characters (*_~`) that we just inserted above.
        # Strategy: split around already-formatted spans, escape only the gaps.
        _span_re = re.compile(
            r"\*[^*\n]+\*"           # bold
            r"|_[^_\n]+_"            # italic
            r"|~[^~\n]+~"            # strikethrough
            r"|`[^`]+`"              # inline code
            r"|\[[^\]]+\]\([^)]+\)"  # link
        )
        parts = _span_re.split(segment)
        spans = _span_re.findall(segment)
        merged: list[str] = []
        for i, part in enumerate(parts):
            merged.append(_escape_telegram(part))
            if i < len(spans):
                merged.append(spans[i])
        segment = "".join(merged)

        result.append(segment)

    return "".join(result)


# ---------------------------------------------------------------------------
# Slack mrkdwn
# ---------------------------------------------------------------------------

def convert_md_to_slack_mrkdwn(text: str) -> str:
    """Converts standard Markdown to Slack mrkdwn format.

    Handles: bold (**), italic (*/_), inline code (`), code blocks (```),
    links ([text](url)), strikethrough (~~), and headings.

    Args:
        text: Input text in standard Markdown format.

    Returns:
        Text formatted as Slack mrkdwn.
    """
    # Fenced code blocks — keep as-is (Slack renders ``` natively)
    # Bold **text** or __text__ → *text*
    # Use null-byte placeholders to protect bold output from the italic pass.
    _B0 = "\x00B\x01"
    _B1 = "\x02"
    result = re.sub(r"\*\*(.+?)\*\*", lambda m: f"{_B0}{m.group(1)}{_B1}", text)
    result = re.sub(r"__(.+?)__", lambda m: f"{_B0}{m.group(1)}{_B1}", result)
    # Italic *text* → _text_  (Slack italic is _text_) — bold placeholders not matched
    result = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", result)
    # Restore bold placeholders
    result = result.replace(_B0, "*").replace(_B1, "*")
    # Italic _text_ — already Slack-compatible, leave as-is
    # Strikethrough ~~text~~ → ~text~
    result = re.sub(r"~~(.+?)~~", r"~\1~", result)
    # Links [text](url) → <url|text>
    result = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", result)
    # Headings — strip # prefix (Slack has no heading syntax)
    result = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", result, flags=re.MULTILINE)
    return result


# ---------------------------------------------------------------------------
# Plain text (strip all Markdown)
# ---------------------------------------------------------------------------

def strip_markdown(text: str) -> str:
    """Removes all Markdown formatting, returning plain text.

    Strips: headings, bold, italic, strikethrough, inline code,
    fenced code blocks, links, images, blockquotes, and horizontal rules.

    Args:
        text: Input text potentially containing Markdown formatting.

    Returns:
        Plain text with all Markdown syntax removed.
    """
    # Fenced code blocks — keep content, remove fences
    result = re.sub(r"```(?:[^\n]*\n)?([\s\S]*?)```", r"\1", text)
    # Inline code
    result = re.sub(r"`([^`]+)`", r"\1", result)
    # Headings
    result = re.sub(r"^#{1,6}\s+", "", result, flags=re.MULTILINE)
    # Bold and italic (order: bold first to avoid partial matches)
    result = re.sub(r"\*{3}(.+?)\*{3}", r"\1", result)
    result = re.sub(r"\*\*(.+?)\*\*", r"\1", result)
    result = re.sub(r"__(.+?)__", r"\1", result)
    result = re.sub(r"\*(.+?)\*", r"\1", result)
    result = re.sub(r"_(.+?)_", r"\1", result)
    # Strikethrough
    result = re.sub(r"~~(.+?)~~", r"\1", result)
    # Images — remove entirely
    result = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", result)
    # Links — keep link text
    result = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", result)
    # Blockquotes
    result = re.sub(r"^>\s+", "", result, flags=re.MULTILINE)
    # Horizontal rules
    result = re.sub(r"^[-*_]{3,}\s*$", "", result, flags=re.MULTILINE)
    # Collapse multiple blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
