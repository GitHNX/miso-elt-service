"""
Reporting API — FastAPI application.

Security model
──────────────
- Connects to PostgreSQL as `miso_readonly` (SELECT-only role)
- API key authentication via Bearer token (injected from Secrets Manager)
- Rate limiting via slowapi (60 req/min per IP on data endpoints)
- Max date-range window on history queries (31 days) to prevent expensive scans
- Security headers on every response (X-Content-Type-Options, X-Frame-Options, etc.)
- Never exposes DB credentials or internal stack traces to callers
- Deployed behind an ALB; the ALB health-check endpoint (/health) is
  unauthenticated so the ALB target group can probe it

Endpoints
─────────
GET /health                     — ALB health check (no auth)
GET /api/v1/fuel-mix/latest     — most recent snapshot
GET /api/v1/fuel-mix/history    — paginated history with optional filters
GET /api/v1/fuel-mix/summary    — aggregated stats per fuel type
GET /api/v1/ingestion/status    — last N ingestion runs (operational view)
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Callable, Coroutine, Generator, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.config import get_settings
from src.core.logging import configure_logging, get_logger
from src.db.session import ReadonlySession, check_db_connectivity
from src.models.orm import DimFuelCategory, FactFuelMix, IngestionRun

configure_logging()
logger = get_logger(__name__)
settings = get_settings()

# ── Rate limiter ──────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="MISO Fuel Mix API",
    description="Read-only access to MISO real-time fuel mix data",
    version="1.0.0",
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.environment != "production" else None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["Authorization"],
)

# ── Security headers middleware ───────────────────────────────────────────────

@app.middleware("http")
async def add_security_headers(
    request: Request,
    call_next: Callable[[Request], Coroutine[Any, Any, JSONResponse]],
) -> JSONResponse:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


# ── Auth ──────────────────────────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=True)


def require_api_key(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> None:
    """Constant-time comparison prevents timing attacks."""
    if not secrets.compare_digest(credentials.credentials, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── DB dependency ─────────────────────────────────────────────────────────────

def get_db() -> Generator[Session, None, None]:
    session = ReadonlySession()
    try:
        yield session
    finally:
        session.close()


DbDep = Annotated[Session, Depends(get_db)]
AuthDep = Annotated[None, Depends(require_api_key)]

# Max date range for history queries — prevents full-table scans
_MAX_HISTORY_DAYS = 31


# ── Response schemas ──────────────────────────────────────────────────────────

class FuelTypeReading(BaseModel):
    category: str
    act_mw: float
    is_renewable: bool


class FuelMixSnapshot(BaseModel):
    interval_utc: datetime
    ref_id: Optional[str]
    total_mw: Optional[float]
    readings: list[FuelTypeReading]


class HistoryRow(BaseModel):
    interval_utc: datetime
    category: str
    act_mw: float
    is_renewable: bool
    total_mw: Optional[float]


class HistoryResponse(BaseModel):
    page: int
    page_size: int
    total: int
    data: list[HistoryRow]


class FuelSummary(BaseModel):
    category: str
    is_renewable: bool
    avg_mw: float
    max_mw: float
    min_mw: float
    reading_count: int


class IngestionRunRow(BaseModel):
    id: int
    started_at: datetime
    finished_at: Optional[datetime]
    status: str
    rows_upserted: Optional[int]
    error_message: Optional[str]
    interval_est_utc: Optional[datetime]


class HealthResponse(BaseModel):
    status: str
    db_connected: bool
    timestamp: datetime


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["Ops"])
def health() -> HealthResponse:
    """ALB / ECS health check — no authentication required."""
    db_ok = check_db_connectivity()
    return HealthResponse(
        status="ok" if db_ok else "degraded",
        db_connected=db_ok,
        timestamp=datetime.now(timezone.utc),
    )


@app.get(
    "/api/v1/fuel-mix/latest",
    response_model=FuelMixSnapshot,
    tags=["Fuel Mix"],
)
@limiter.limit("60/minute")
def get_latest(request: Request, db: DbDep, _: AuthDep) -> FuelMixSnapshot:
    """Return the most recent fuel-mix snapshot across all fuel types."""
    latest_interval = db.query(func.max(FactFuelMix.interval_est_utc)).scalar()
    if not latest_interval:
        raise HTTPException(status_code=404, detail="No data available yet")

    rows = (
        db.query(FactFuelMix, DimFuelCategory)
        .join(DimFuelCategory, FactFuelMix.fuel_category_id == DimFuelCategory.id)
        .filter(FactFuelMix.interval_est_utc == latest_interval)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No data available yet")

    first_fact = rows[0][0]
    return FuelMixSnapshot(
        interval_utc=latest_interval,
        ref_id=first_fact.raw_ref_id,
        total_mw=float(first_fact.total_mw) if first_fact.total_mw else None,
        readings=[
            FuelTypeReading(
                category=dim.category_name,
                act_mw=float(fact.act_mw),
                is_renewable=dim.is_renewable,
            )
            for fact, dim in rows
        ],
    )


@app.get(
    "/api/v1/fuel-mix/history",
    response_model=HistoryResponse,
    tags=["Fuel Mix"],
)
@limiter.limit("30/minute")
def get_history(
    request: Request,
    db: DbDep,
    _: AuthDep,
    from_utc: Optional[datetime] = Query(None, description="Start of time range (UTC)"),
    to_utc: Optional[datetime] = Query(None, description="End of time range (UTC)"),
    category: Optional[str] = Query(None, description="Filter by fuel category name"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
) -> HistoryResponse:
    """Paginated fuel-mix history with optional time-range and category filters.
    Maximum query window is 31 days.
    """
    # Enforce max date range to prevent expensive full-table scans
    if from_utc and to_utc:
        if (to_utc - from_utc) > timedelta(days=_MAX_HISTORY_DAYS):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Date range cannot exceed {_MAX_HISTORY_DAYS} days.",
            )
    # If only from_utc provided, cap the range automatically
    if from_utc and not to_utc:
        to_utc = from_utc + timedelta(days=_MAX_HISTORY_DAYS)

    q = (
        db.query(FactFuelMix, DimFuelCategory)
        .join(DimFuelCategory, FactFuelMix.fuel_category_id == DimFuelCategory.id)
    )
    if from_utc:
        q = q.filter(FactFuelMix.interval_est_utc >= from_utc)
    if to_utc:
        q = q.filter(FactFuelMix.interval_est_utc <= to_utc)
    if category:
        q = q.filter(DimFuelCategory.category_name.ilike(f"%{category}%"))

    total = q.count()
    rows = (
        q.order_by(FactFuelMix.interval_est_utc.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return HistoryResponse(
        page=page,
        page_size=page_size,
        total=total,
        data=[
            HistoryRow(
                interval_utc=fact.interval_est_utc,
                category=dim.category_name,
                act_mw=float(fact.act_mw),
                is_renewable=dim.is_renewable,
                total_mw=float(fact.total_mw) if fact.total_mw else None,
            )
            for fact, dim in rows
        ],
    )


@app.get(
    "/api/v1/fuel-mix/summary",
    response_model=list[FuelSummary],
    tags=["Fuel Mix"],
)
@limiter.limit("60/minute")
def get_summary(
    request: Request,
    db: DbDep,
    _: AuthDep,
    from_utc: Optional[datetime] = Query(None),
    to_utc: Optional[datetime] = Query(None),
) -> list[FuelSummary]:
    """Aggregate stats (avg / min / max MW) per fuel category over a time range."""
    if from_utc and to_utc:
        if (to_utc - from_utc) > timedelta(days=_MAX_HISTORY_DAYS):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Date range cannot exceed {_MAX_HISTORY_DAYS} days.",
            )

    q = db.query(
        DimFuelCategory.category_name,
        DimFuelCategory.is_renewable,
        func.avg(FactFuelMix.act_mw).label("avg_mw"),
        func.max(FactFuelMix.act_mw).label("max_mw"),
        func.min(FactFuelMix.act_mw).label("min_mw"),
        func.count(FactFuelMix.id).label("reading_count"),
    ).join(DimFuelCategory, FactFuelMix.fuel_category_id == DimFuelCategory.id)

    if from_utc:
        q = q.filter(FactFuelMix.interval_est_utc >= from_utc)
    if to_utc:
        q = q.filter(FactFuelMix.interval_est_utc <= to_utc)

    rows = q.group_by(DimFuelCategory.category_name, DimFuelCategory.is_renewable).all()
    return [
        FuelSummary(
            category=r.category_name,
            is_renewable=r.is_renewable,
            avg_mw=round(float(r.avg_mw), 2),
            max_mw=float(r.max_mw),
            min_mw=float(r.min_mw),
            reading_count=r.reading_count,
        )
        for r in rows
    ]


@app.get(
    "/api/v1/ingestion/status",
    response_model=list[IngestionRunRow],
    tags=["Ops"],
)
@limiter.limit("60/minute")
def get_ingestion_status(
    request: Request,
    db: DbDep,
    _: AuthDep,
    limit: int = Query(20, ge=1, le=200),
) -> list[IngestionRunRow]:
    """Return the last N ingestion run records (for operational dashboards)."""
    runs = (
        db.query(IngestionRun)
        .order_by(IngestionRun.started_at.desc())
        .limit(limit)
        .all()
    )
    return [
        IngestionRunRow(
            id=r.id,  # type: ignore[arg-type]
            started_at=r.started_at,  # type: ignore[arg-type]
            finished_at=r.finished_at,  # type: ignore[arg-type]
            status=r.status,  # type: ignore[arg-type]
            rows_upserted=r.rows_upserted,  # type: ignore[arg-type]
            error_message=r.error_message,  # type: ignore[arg-type]
            interval_est_utc=r.interval_est_utc,  # type: ignore[arg-type]
        )
        for r in runs
    ]