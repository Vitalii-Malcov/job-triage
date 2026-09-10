"""Shared MIME payload decoding for email content.

Pure `email.message.Message` decoding, with no provider-specific parsing
logic of its own -- extracted after `app.providers.email.imap` (Gmail IMAP)
and `app.collectors.xing_email` (XING digest IMAP) had accumulated
byte-identical copies of this same charset-decode-with-fallback. Sharing it
does not couple the two collectors/providers to each other: neither depends
on the other's parsing decisions, only on the same stdlib-adjacent decoding
step.
"""

from email.message import Message


def decode_mime_part(part: Message) -> str:
    """Decode one MIME part's payload to text, using its declared charset
    (falling back to utf-8 if that charset is unknown/unsupported), with
    `errors="replace"` so malformed bytes never raise. Returns "" if the
    part has no payload at all.
    """
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")
