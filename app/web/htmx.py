"""Answers that drive htmx: redirects and client events.

Every answer built here carries ``Cache-Control: no-store`` and ``Vary: HX-Request``, like the
pages and fragments the application renders, so a shared cache never keeps one.
"""

import json
from collections.abc import Mapping

from fastapi import Response

ANSWER_HEADERS = {"Cache-Control": "no-store", "Vary": "HX-Request"}


def hx_events(*names: str) -> str:
    """An ``HX-Trigger`` value firing the named client events."""
    return json.dumps(dict.fromkeys(names, True))


def events_response(*names: str) -> Response:
    """An empty 204 answer that fires the named client events."""
    return Response(status_code=204, headers={**ANSWER_HEADERS, "HX-Trigger": hx_events(*names)})


def hx_redirect(
    url: str, *, status_code: int = 200, headers: Mapping[str, str] | None = None
) -> Response:
    """An empty answer that tells htmx to navigate to ``url``.

    The response is returned open, so a caller can still set a cookie on it.
    """
    return Response(
        status_code=status_code,
        headers={**(headers or {}), **ANSWER_HEADERS, "HX-Redirect": url},
    )
