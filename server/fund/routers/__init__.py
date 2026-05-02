"""Router package for fund API.

The investor-verification router (prompt 26) is exposed as
``investor_verification_router`` for callers that mount routers
explicitly (``server/fund/app.py``). Importing it lazily keeps
test fixtures that mount only a subset of routers cheap.
"""

from coherence_engine.server.fund.routers.investor_verification import (
    router as investor_verification_router,
)


__all__ = ["investor_verification_router"]
