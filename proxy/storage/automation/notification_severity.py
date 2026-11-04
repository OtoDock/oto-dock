"""A notification's severity vocabulary: ``notifications.severity`` and
``notification_deliveries.severity`` named once.

Four stored values, every spelling frozen (the columns, the notification
frame's and the push payload's ``severity``, the notifications, triggers and
schedules MCPs' texts, an app handler's ``notifications.create``): ``info``,
``success``, ``warning`` and ``danger``. ``danger`` is the alarm: the
dashboard loops its alarm and speaks the notification until it is dismissed,
and an iOS push of it is time-sensitive, so it passes Focus. The schedules
MCP offers the first three alone for a task's generic Task Complete ping.

Every write path takes :func:`normalize` (any other word, or none, is
``info``); generic code asks :func:`is_alarm`. The dashboard mirrors the
words in ``dashboard/src/lib/status/notificationSeverity.ts``
(``tests/storage/test_status_vocabularies.py`` binds the two).

Stdlib only: a leaf every side imports at module level, module-qualified
(``from storage.automation import notification_severity``).
"""

from __future__ import annotations

INFO = "info"
SUCCESS = "success"
WARNING = "warning"
DANGER = "danger"

#: In the order a picker lists them, the mildest first.
SEVERITIES: tuple[str, ...] = (INFO, SUCCESS, WARNING, DANGER)


def normalize(value: object) -> str:
    """The stored severity of ``value``: one of :data:`SEVERITIES`, else ``info``."""
    return value if isinstance(value, str) and value in SEVERITIES else INFO


def is_alarm(severity: object) -> bool:
    """Whether ``severity`` is the alarm (``danger``)."""
    return severity == DANGER
