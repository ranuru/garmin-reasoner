"""Authentication for admin routes.

HTTP Basic with a single shared password (`ADMIN_PASSWORD`); the username is ignored.
Basic auth works with plain browser navigation, which `/connect` needs. This is defence
in depth: admin routes are also meant to be reachable only over the tailnet.
"""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

basic = HTTPBasic(realm="garmin-reasoner admin")


def require_admin(
    request: Request, credentials: Annotated[HTTPBasicCredentials, Depends(basic)]
) -> None:
    expected = request.app.state.settings.admin_password.get_secret_value()
    if not secrets.compare_digest(credentials.password.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
