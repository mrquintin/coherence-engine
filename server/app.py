"""FastAPI server for the Coherence Engine (optional)."""

try:
    from fastapi import FastAPI
    from pydantic import BaseModel
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


def create_app():
    """Create and configure the FastAPI application."""
    if not FASTAPI_AVAILABLE:
        raise ImportError("FastAPI is required: pip install fastapi uvicorn")

    from coherence_engine import CoherenceScorer, __version__

    app = FastAPI(
        title="Coherence Engine API",
        version=__version__,
        description="Measure the internal logical coherence of text.",
    )

    scorer = CoherenceScorer()

    class AnalyzeRequest(BaseModel):
        text: str
        format: str = "json"

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/layers")
    def layers():
        return {"layers": ["contradiction", "argumentation", "embedding", "compression", "structural"]}

    @app.post("/analyze")
    def analyze(req: AnalyzeRequest):
        result = scorer.score(req.text)
        return result.to_dict()

    return app


if __name__ == "__main__":
    import uvicorn
    app = create_app()
    uvicorn.run(app, host="0.0.0.0", port=8000)
