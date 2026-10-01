"""Security alert hooks (key suspended, and later: new login, key connected).

A plain hook list so the sender is pluggable: the default hook logs; the
email module registers `send_security_alert` at startup (integration). A hook
that raises is logged and skipped, so one broken channel never stops the
others or the job that raised the alert. Alerts never carry secrets.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecurityAlert:
    kind: str  # "key_suspended" | ...
    user_id: int | None
    subject: str
    body: str
    details: dict[str, Any] = field(default_factory=dict)


AlertHook = Callable[[SecurityAlert], None]


def log_alert(alert: SecurityAlert) -> None:
    logger.warning("security alert %s for user %s: %s", alert.kind, alert.user_id, alert.subject)


HOOKS: list[AlertHook] = [log_alert]


def register(hook: AlertHook) -> None:
    if hook not in HOOKS:
        HOOKS.append(hook)


def unregister(hook: AlertHook) -> None:
    if hook in HOOKS:
        HOOKS.remove(hook)


def emit(alert: SecurityAlert) -> int:
    """Call every hook; returns how many succeeded."""
    delivered = 0
    for hook in list(HOOKS):
        try:
            hook(alert)
            delivered += 1
        except Exception:
            logger.exception("alert hook %r failed for %s", hook, alert.kind)
    return delivered
