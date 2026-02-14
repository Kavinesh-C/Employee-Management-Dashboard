"""
CORS SECURITY
=============
CORS middleware helper for API hardening.
"""

# FLOW:
# - add_cors(app, origins) configures CORS once at startup.
# WHY:
# - Controls which web origins can access the API.
# HOW:
# - Adds FastAPI CORSMiddleware with allowed origins.

from __future__ import annotations

from fastapi.middleware.cors import CORSMiddleware


def add_cors(app, origins: list[str]):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=["*"]
    )
