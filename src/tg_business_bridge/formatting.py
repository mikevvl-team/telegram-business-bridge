"""Разметка черновиков: обычный текст и Telegram HTML в одном представлении."""
import html
import re

HTML = "HTML"

_TAG_RE = re.compile(r"<[^>]+>")


def visible_text(text: str, parse_mode: str | None) -> str:
    """Текст, который увидит собеседник. Лимит Telegram в 4096 символов считается
    именно по нему, а не по исходнику с тегами: длинный href видимую длину не ест."""
    if parse_mode != HTML:
        return text
    return html.unescape(_TAG_RE.sub("", text))


def to_html(text: str, parse_mode: str | None) -> str:
    """Черновик в виде Telegram HTML. Обычный текст экранируется: всё, что уходит
    владельцу и собеседнику, отправляется с parse_mode='HTML'."""
    if parse_mode == HTML:
        return text
    return html.escape(text, quote=False)
