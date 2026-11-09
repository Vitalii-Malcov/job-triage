"""Link safety shared by the UI query modules."""


def safe_external_url(url: str | None) -> str | None:
    """Only http(s) URLs become links; the UI never fetches them."""
    if not url:
        return None
    lowered = url.strip().lower()
    if lowered.startswith(("http://", "https://")) and len(lowered) > len("https://"):
        return url.strip()
    return None
