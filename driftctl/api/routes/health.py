"""driftctl/api/routes/health.py"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/health", tags=["health"])
async def health_check():
    """Health check — no auth required."""
    return JSONResponse({"status": "ok", "version": "1.0.0"})
