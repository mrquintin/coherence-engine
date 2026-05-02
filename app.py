"""Vercel FastAPI entrypoint for the Coherence Fund backend."""

from coherence_engine.server.fund.app import create_app


app = create_app()
