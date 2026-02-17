from fastapi import APIRouter, Depends, Header, HTTPException

from app.dependencies import get_ibkr_client, get_settings
from app.services.ibkr_client import IBKRClient

router = APIRouter(prefix="/api/v1/positions", tags=["positions"])


@router.get("/")
async def get_positions(
    x_api_key: str = Header(...),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    settings=Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    positions = await ibkr_client.get_open_positions()
    return {"positions": positions}


@router.get("/account")
async def get_account(
    x_api_key: str = Header(...),
    ibkr_client: IBKRClient = Depends(get_ibkr_client),
    settings=Depends(get_settings),
):
    if x_api_key != settings.api_secret_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    account = await ibkr_client.get_account_info()
    return {"account": account}
