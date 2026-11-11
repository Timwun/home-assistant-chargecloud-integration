"""EVSE ID syntax helpers."""

from __future__ import annotations

import re

EVSE_ID_PATTERN = (
    r"^(([A-Z]{2}\*?[A-Z0-9]{3}\*?E[A-Z0-9\*]{1,30})"
    r"|(\+?[0-9]{1,3}\*[0-9]{3}\*[0-9\*]{1,32}))$"
)


def is_valid_evse_id(value: str) -> bool:
    """Return whether a string has the supported EVSE-ID syntax."""
    return re.fullmatch(EVSE_ID_PATTERN, value) is not None
