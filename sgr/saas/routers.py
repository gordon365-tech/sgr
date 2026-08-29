"""
SGR SaaS API Routers
====================
Authentifizierung, Billing und API-Key-Management.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr

from sgr.api.dependencies import TokenData, require_auth
from sgr.core.types import TradingMode
from sgr.saas.auth import AuthService
from sgr.saas.fees import PerformanceFeeEngine

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    trading_mode: str = "paper"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp_code: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user_id: str
    trading_mode: str


class RefreshRequest(BaseModel):
    refresh_token: str


class Setup2FAResponse(BaseModel):
    totp_uri: str
    secret: str
    message: str


class Enable2FARequest(BaseModel):
    totp_code: str


class StoreAPIKeyRequest(BaseModel):
    exchange: str
    trading_mode: str
    api_key: str
    secret: str
    label: str = "default"


class PerformanceReportResponse(BaseModel):
    user_id: str
    report_generated_at: str
    summary: dict
    portfolio_history: list
    fee_periods: list


# ---------------------------------------------------------------------------
# Auth Router
# ---------------------------------------------------------------------------

auth_router = APIRouter(prefix="/auth", tags=["auth"])
_auth = AuthService()


@auth_router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, request: Request) -> TokenResponse:
    """
    Registriert neuen User.
    Startet immer im Paper-Trading-Modus.
    Live-Trading muss separat freigeschaltet werden (2FA + Verification).
    """
    try:
        mode = TradingMode(body.trading_mode)
        if mode == TradingMode.LIVE:
            raise HTTPException(
                status_code=400,
                detail="New accounts start in paper mode. Upgrade to live after 2FA setup.",
            )
        result = await _auth.register_user(body.email, body.password, mode)
        return TokenResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@auth_router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request) -> TokenResponse:
    """
    Login mit Email + Passwort.
    Falls 2FA aktiviert: totp_code erforderlich.
    """
    try:
        ip = request.client.host if request.client else "unknown"
        result = await _auth.login(
            email=body.email,
            password=body.password,
            totp_code=body.totp_code,
            ip_address=ip,
        )
        return TokenResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


@auth_router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: RefreshRequest) -> TokenResponse:
    """Erneuert Access Token via Refresh Token."""
    try:
        user_id = _auth.verify_refresh_token(body.refresh_token)
        from sgr.core.repositories import get_repositories

        repos = get_repositories()
        await repos.users.get_by_email("")  # Placeholder
        # In Produktion: User aus DB laden per user_id
        mode = TradingMode.PAPER
        access_token = _auth.create_access_token(user_id, mode)
        new_refresh = _auth.create_refresh_token(user_id)
        return TokenResponse(
            access_token=access_token,
            refresh_token=new_refresh,
            token_type="bearer",
            user_id=user_id,
            trading_mode=mode.value,
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


@auth_router.post("/2fa/setup", response_model=Setup2FAResponse)
async def setup_2fa(
    user: Annotated[TokenData, Depends(require_auth)],
) -> Setup2FAResponse:
    """
    Initiiert 2FA Setup.
    Returns TOTP URI für QR-Code.
    User muss danach /2fa/enable mit Code aufrufen.
    """
    from sgr.core.repositories import get_repositories

    get_repositories()

    # Email für TOTP URI laden
    # In Produktion: aus DB per user_id
    email = f"user_{user.user_id}@sgr.app"

    result = await _auth.setup_2fa(user.user_id, email)
    return Setup2FAResponse(
        totp_uri=result["totp_uri"],
        secret=result["secret"],
        message="Scan the QR code with your authenticator app, then call /2fa/enable",
    )


@auth_router.post("/2fa/enable")
async def enable_2fa(
    body: Enable2FARequest,
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """Aktiviert 2FA nach Bestätigung des TOTP-Codes."""
    success = await _auth.enable_2fa(user.user_id, body.totp_code)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid TOTP code or 2FA not set up")
    return {"2fa_enabled": True, "message": "2FA is now active for your account"}


# ---------------------------------------------------------------------------
# API Key Management Router
# ---------------------------------------------------------------------------

apikey_router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@apikey_router.post("/")
async def store_api_key(
    body: StoreAPIKeyRequest,
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """
    Speichert verschlüsselten Exchange API Key.
    Key wird AES-256-GCM verschlüsselt mit User-ID als AAD.
    """
    from sgr.saas.tenant import get_tenant_manager

    manager = get_tenant_manager()

    try:
        trading_mode = TradingMode(body.trading_mode)
        key_id = await manager.store_api_key(
            user_id=user.user_id,
            exchange_id=body.exchange,
            trading_mode=trading_mode,
            api_key=body.api_key,
            secret=body.secret,
            label=body.label,
        )
        return {
            "key_id": key_id,
            "exchange": body.exchange,
            "trading_mode": body.trading_mode,
            "label": body.label,
            "message": "API key stored securely",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@apikey_router.get("/")
async def list_api_keys(
    user: Annotated[TokenData, Depends(require_auth)],
) -> list[dict]:
    """
    Listet alle konfigurierten API Keys (ohne Secrets!).
    """
    from sqlalchemy import select

    from sgr.core.database import APIKeyModel, get_session

    async with get_session() as session:
        result = await session.execute(
            select(APIKeyModel).where(APIKeyModel.user_id == user.user_id)
        )
        keys = result.scalars().all()

    return [
        {
            "id": str(k.id),
            "exchange": k.exchange,
            "trading_mode": k.trading_mode,
            "label": k.label,
            "is_active": k.is_active,
            "created_at": k.created_at.isoformat(),
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            # api_key und secret werden NIEMALS zurückgegeben
        }
        for k in keys
    ]


@apikey_router.delete("/{key_id}")
async def delete_api_key(
    key_id: str,
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """Deaktiviert API Key (nicht wirklich gelöscht – Audit Trail)."""
    from sqlalchemy import and_, update

    from sgr.core.database import APIKeyModel, get_session

    async with get_session() as session:
        result = await session.execute(
            update(APIKeyModel)
            .where(
                and_(
                    APIKeyModel.id == key_id,
                    APIKeyModel.user_id == user.user_id,
                )
            )
            .values(is_active=False)
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="API key not found")

    return {"deleted": key_id}


# ---------------------------------------------------------------------------
# Billing Router
# ---------------------------------------------------------------------------

billing_router = APIRouter(prefix="/billing", tags=["billing"])
_fee_engine = PerformanceFeeEngine()


@billing_router.get("/performance-report")
async def get_performance_report(
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """
    Vollständiger Performance + Fee Report.
    Zeigt HWM-Verlauf, berechnete Fees, Portfolio-Entwicklung.
    """
    # In Produktion: aus DB laden
    report = _fee_engine.generate_performance_report(
        user_id=user.user_id,
        calculations=[],
        snapshots=[],
    )
    return report


@billing_router.get("/fees")
async def get_fee_summary(
    user: Annotated[TokenData, Depends(require_auth)],
) -> dict:
    """
    Fee-Zusammenfassung: ausstehende und bezahlte Fees.
    """
    hwm = await _fee_engine.get_hwm(
        user.user_id, initial_capital=__import__("decimal").Decimal("10000")
    )
    return {
        "user_id": user.user_id,
        "current_hwm_usdt": str(hwm.current_hwm),
        "cumulative_fees_paid_usdt": str(hwm.cumulative_fees_paid),
        "fee_rate": "5%",
        "fee_model": "High-Water-Mark",
        "next_billing_date": "End of current month",
        "currency": hwm.currency,
    }


@billing_router.get("/invoices")
async def get_invoices(
    user: Annotated[TokenData, Depends(require_auth)],
) -> list[dict]:
    """Alle Rechnungen für den User."""
    # In Produktion: aus DB laden
    return []
