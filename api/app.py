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

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.routes import listings, search
from api.routes.geocode import router as geocode_router

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
app.include_router(geocode_router, prefix="/api/v1")

_STATIC_DIR = Path(__file__).parent.parent / "static"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


# Mount static assets (CSS, JS, images) after explicit routes
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
