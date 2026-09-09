import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # AUD-002: httpx (and python-telegram-bot, which sends its own
    # requests through an internal httpx.AsyncClient) logs every request
    # at INFO level as `HTTP Request: %s %s "%s %d %s"` with the FULL
    # request URL -- for a Telegram Bot API call, that URL embeds the
    # bot token itself (https://api.telegram.org/bot<TOKEN>/sendMessage).
    # At root level INFO, that record would propagate to the root
    # handler and be written out, leaking the token. Fixed centrally here
    # (not via each call site's own logger) so no future httpx caller
    # anywhere in this project can reintroduce the leak by omission.
    # httpcore is raised too, defense-in-depth, in case its own DEBUG
    # request/header logging is ever enabled by a lower root level.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
