"""Technical analysis API endpoints."""

from fastapi import APIRouter, Depends, Header, HTTPException

from app.config import Settings
from app.dependencies import get_settings, get_technical_analyzer
from app.services.technical_analyzer import TechnicalAnalyzer

router = APIRouter(prefix="/api/v1/technicals", tags=["technicals"])


@router.get("/scan")
async def scan_all(
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
    analyzer: TechnicalAnalyzer = Depends(get_technical_analyzer),
):
    """Scan all instruments and return ranked by score."""
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return await analyzer.scan_all()


@router.get("/{instrument}")
async def analyze_instrument(
    instrument: str,
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
    analyzer: TechnicalAnalyzer = Depends(get_technical_analyzer),
):
    """Full multi-timeframe analysis for a single instrument."""
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    result = await analyzer.analyze(instrument)
    if "error" in result and "available" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result


@router.get("/{instrument}/intraday")
async def analyze_instrument_intraday(
    instrument: str,
    x_api_key: str = Header(...),
    settings: Settings = Depends(get_settings),
    analyzer: TechnicalAnalyzer = Depends(get_technical_analyzer),
):
    """Intraday/scalp analysis using 1H and 15m timeframes."""
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    result = await analyzer.analyze_intraday(instrument)
    if "error" in result and "available" in result:
        raise HTTPException(status_code=404, detail=result["error"])

    return result
