"""Generic, stateless text-bounding helpers with no domain knowledge of
their own -- shared only because the mechanics were byte-identical across
independent call sites, each of which owns its own bound.
"""


def truncate_with_ellipsis(text: str, max_length: int) -> str:
    """Strips `text`, then bounds it to `max_length` characters, appending
    "..." if it had to cut anything. `max_length` is the caller's own
    domain-specific bound (e.g. an evidence-fragment or reply-list length
    cap) -- this function has no opinion on what that bound should be.
    """
    stripped = text.strip()
    if len(stripped) <= max_length:
        return stripped
    return stripped[:max_length].rstrip() + "..."
