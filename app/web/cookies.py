"""The cookies the web routes hand to the browser.

Every cookie that carries a credential (the session, the pending sign-in, the pending two-factor
setup) is set here, so none of them can lose the HttpOnly flag or forget to be Secure.
"""

from typing import Literal

from starlette.responses import Response

from app.config import get_settings


def set_private_cookie(
    response: Response,
    name: str,
    value: str,
    *,
    max_age: int,
    path: str,
    samesite: Literal["lax", "strict"],
) -> None:
    """Set an HttpOnly cookie, Secure when the configured base URL is https."""
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=get_settings().base_url.startswith("https://"),
        samesite=samesite,
        path=path,
    )
