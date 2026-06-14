"""
driftctl/api/middleware.py

Optional API key authentication middleware.

When api_key is set in driftctl.yaml, all endpoints except /health
require the header: X-API-Key: <value>
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid X-API-Key header."""

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self._api_key = api_key

    async def dispatch(self, request: Request, call_next):
        # Health check is always public
        if request.url.path == "/health":
            return await call_next(request)

        provided = request.headers.get("X-API-Key", "")
        if provided != self._api_key:
            return JSONResponse(
                status_code=401,
                content={
                    "data": None,
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "Valid X-API-Key header required",
                    },
                },
            )
        return await call_next(request)
