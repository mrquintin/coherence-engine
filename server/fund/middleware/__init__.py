"""API gateway middleware (prompt 37).

Public surface:

- :func:`install_gateway_middleware` — wire the request-id, CORS,
  signing, and rate-limit layers onto a FastAPI app in the right order.
- :data:`RATE_LIMITER` — process-local fallback bucket registry, exposed
  for tests that need to reset state between cases.

The four concerns each live in their own module so they can be tested
in isolation; this package only stitches them together.
"""

from __future__ import annotations

from .cors import install_cors
from .rate_limit import RATE_LIMITER, RateLimitMiddleware
from .request_id import RequestIdMiddleware
from .request_signing import RequestSigningMiddleware


def install_gateway_middleware(app) -> None:
    """Install request-id, CORS, signing, and rate-limit middleware.

    Order matters: outermost middleware runs first on inbound requests
    and last on outbound responses. We want request-id assignment to
    wrap everything (so 4xx denials still echo a request id), then the
    CORS layer (so preflights short-circuit early), then signing, then
    rate-limit closest to the route.
    """

    # ``add_middleware`` prepends to the stack, so the *last* call ends
    # up outermost. We add in reverse-order of intended execution.
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestSigningMiddleware)
    install_cors(app)
    app.add_middleware(RequestIdMiddleware)


__all__ = [
    "RATE_LIMITER",
    "RateLimitMiddleware",
    "RequestIdMiddleware",
    "RequestSigningMiddleware",
    "install_cors",
    "install_gateway_middleware",
]
