"""
FastAPI web layer — future web/iOS backend.

Start with: python main.py serve
or directly: uvicorn api.app:app --reload

The engine is the same object the CLI uses.  The web layer is just a
thin HTTP wrapper around it — no business logic lives here.

Endpoints (stub — ready to implement):
  GET  /api/v1/profiles           → list all search profiles
  POST /api/v1/profiles           → create a new profile (iOS "New Search" form)
  GET  /api/v1/profiles/{name}    → get a single profile
  PUT  /api/v1/profiles/{name}    → update a profile
  DELETE /api/v1/profiles/{name}  → delete a profile
  POST /api/v1/search             → run a search now (returns MatchResult list)
  GET  /api/v1/results            → paginated history of all past matches
  GET  /health                    → liveness probe
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import listings, search

app = FastAPI(
    title="find-a-home",
    description="Real estate deal finder with PITI analysis and assumable-loan detection.",
    version="1.0.0",
)

# Allow cross-origin requests from the future web/mobile front-end
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(search.router, prefix="/api/v1")
app.include_router(listings.router, prefix="/api/v1")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
