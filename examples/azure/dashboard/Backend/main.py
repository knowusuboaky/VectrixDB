"""
Root-level entry point for convenience.

Allows running with either:
    uvicorn main:app --reload
    uvicorn app.main:app --reload

Author: Kwadwo Daddy Nyame Owusu - Boakye
"""

from app.main import app

__all__ = ["app"]
