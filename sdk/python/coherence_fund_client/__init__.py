"""Typed Python SDK stub for the Coherence Fund Orchestrator API.

The :mod:`client` module is generated from ``docs/specs/openapi_v1.yaml``
by ``scripts/generate_sdk_stubs.py``. Re-running the generator with an
unchanged YAML produces byte-identical output (prompt 17 reproducibility
guarantee).

Public surface::

    from coherence_fund_client import CoherenceFundClient, CoherenceFundClientError

The client depends only on the Python standard library so it can be
vendored into environments without ``requests``.
"""

from coherence_fund_client.client import (  # noqa: F401
    CoherenceFundClient,
    CoherenceFundClientError,
)

__all__ = ["CoherenceFundClient", "CoherenceFundClientError"]
