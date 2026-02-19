from fastapi import APIRouter

from app.instruments import INSTRUMENTS

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "trader-bot",
        "instruments": [
            {
                "key": spec.key,
                "name": spec.display_name,
                "sec_type": spec.sec_type,
                "exchange": spec.exchange,
            }
            for spec in INSTRUMENTS.values()
        ],
    }
