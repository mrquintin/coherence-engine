"""Security package — middleware, role checks, RLS policy registry.

The middleware / role-check / audit surface lives in ``.middleware`` and is
re-exported lazily via ``__getattr__`` so existing callers keep working
with ``from coherence_engine.server.fund.security import X``. The lazy
export matters: the declarative RLS registry in ``.rls`` has no FastAPI
dependency, and we want migrations / Alembic tooling to be able to import
``security.rls`` without dragging in the full FastAPI stack.
"""

from __future__ import annotations


_AUTH_EXPORTS = {"current_founder", "verify_supabase_jwt", "AuthError"}
_JWKS_EXPORTS = {"JwksCache", "JWKSUnavailable", "get_default_cache"}
# Submodule names — let Python's import machinery resolve these normally
# instead of intercepting them via ``__getattr__``. Otherwise ``from . import
# middleware`` recurses into our shim while it tries to bind the submodule
# attribute back onto the package, and we get a runaway stack.
_SUBMODULES = {"middleware", "auth", "jwks_cache", "rls"}


def __getattr__(name: str):  # pragma: no cover - thin import shim
    if name.startswith("__") or name in _SUBMODULES:
        raise AttributeError(name)
    if name in _AUTH_EXPORTS:
        from . import auth

        return getattr(auth, name)
    if name in _JWKS_EXPORTS:
        from . import jwks_cache

        return getattr(jwks_cache, name)
    from . import middleware

    try:
        return getattr(middleware, name)
    except AttributeError as exc:
        raise AttributeError(
            f"module 'coherence_engine.server.fund.security' has no attribute {name!r}"
        ) from exc
