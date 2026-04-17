"""Compatibility wrapper for fund API app factory."""

from coherence_engine.server.fund.app import create_app as create_fund_app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_fund_app(), host="0.0.0.0", port=8010)

