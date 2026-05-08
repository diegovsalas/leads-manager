"""
API cost tracking. Port directo de vendedores.cloud/api-costs.js.

Una llamada por evento (track_cost). Reportes agregados por servicio,
unidad, día. Convierte USD → MXN con tipo de cambio fijo (MXN_RATE).

Uso desde otros módulos:
    from api_costs import track_cost
    track_cost(service="google_places_api", action="search", cost_usd=0.032)

Es seguro llamarlo sin app context — falla soft (loguea y sigue).
"""

import logging
import os
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func

from extensions import db
from models import ApiCost

log = logging.getLogger("api_costs")

MXN_RATE = float(os.getenv("USD_TO_MXN_RATE", "17.5"))


def track_cost(
    service: str,
    action: Optional[str] = None,
    unit: Optional[str] = None,
    user_id: Optional[str] = None,
    tokens_input: int = 0,
    tokens_output: int = 0,
    cost_usd: float = 0,
    metadata: Optional[dict] = None,
) -> None:
    """Persiste una fila de costo. Tolera fallos (no rompe el caller)."""
    if not service:
        return
    cost_mxn = (cost_usd or 0) * MXN_RATE
    try:
        row = ApiCost(
            service=service, action=action, unit=unit,
            user_id=user_id,
            tokens_input=int(tokens_input or 0),
            tokens_output=int(tokens_output or 0),
            cost_usd=Decimal(str(cost_usd or 0)),
            cost_mxn=Decimal(str(cost_mxn)),
            api_metadata=metadata,
        )
        db.session.add(row)
        db.session.commit()
    except Exception as e:
        try:
            db.session.rollback()
        except Exception:
            pass
        log.warning(f"track_cost error: {e}")


def _date_filter_for_period(period: Optional[str], date_from: Optional[str], date_to: Optional[str]):
    """Devuelve un filtro SQLAlchemy (or None) para aplicar a queries."""
    today = datetime.now(timezone.utc).date()
    if period == "today":
        return ApiCost.created_at >= datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    if period == "week":
        return ApiCost.created_at >= datetime.now(timezone.utc) - timedelta(days=7)
    if period == "month":
        first = today.replace(day=1)
        return ApiCost.created_at >= datetime.combine(first, datetime.min.time(), tzinfo=timezone.utc)
    if date_from and date_to:
        try:
            df = datetime.strptime(date_from[:10], "%Y-%m-%d").date()
            dt = datetime.strptime(date_to[:10], "%Y-%m-%d").date()
            start = datetime.combine(df, datetime.min.time(), tzinfo=timezone.utc)
            end = datetime.combine(dt, datetime.max.time(), tzinfo=timezone.utc)
            return (ApiCost.created_at >= start) & (ApiCost.created_at <= end)
        except (ValueError, TypeError):
            return None
    return None


def get_cost_summary(period: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    f = _date_filter_for_period(period, date_from, date_to)

    def _q():
        q = db.session.query(ApiCost)
        if f is not None:
            q = q.filter(f)
        return q

    total = (
        _q().with_entities(
            func.coalesce(func.sum(ApiCost.cost_usd), 0),
            func.coalesce(func.sum(ApiCost.cost_mxn), 0),
            func.count(ApiCost.id),
        ).first()
    )

    by_service = (
        _q().with_entities(
            ApiCost.service,
            func.coalesce(func.sum(ApiCost.cost_usd), 0),
            func.coalesce(func.sum(ApiCost.cost_mxn), 0),
            func.count(ApiCost.id),
        ).group_by(ApiCost.service)
        .order_by(func.sum(ApiCost.cost_usd).desc()).all()
    )

    by_unit = (
        _q().filter(ApiCost.unit.isnot(None))
        .with_entities(
            ApiCost.unit,
            func.coalesce(func.sum(ApiCost.cost_usd), 0),
            func.coalesce(func.sum(ApiCost.cost_mxn), 0),
            func.count(ApiCost.id),
        ).group_by(ApiCost.unit).all()
    )

    by_day = (
        _q().with_entities(
            func.date(ApiCost.created_at).label("d"),
            func.coalesce(func.sum(ApiCost.cost_usd), 0),
            func.coalesce(func.sum(ApiCost.cost_mxn), 0),
            func.count(ApiCost.id),
        ).group_by("d").order_by(db.desc("d")).limit(31).all()
    )

    # Today / month / chatbot specific
    today = datetime.now(timezone.utc).date()
    today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    month_start = datetime.combine(today.replace(day=1), datetime.min.time(), tzinfo=timezone.utc)

    today_stats = (
        db.session.query(
            func.coalesce(func.sum(ApiCost.cost_mxn), 0),
            func.count(ApiCost.id),
        ).filter(ApiCost.created_at >= today_start).first()
    )
    month_stats = (
        db.session.query(
            func.coalesce(func.sum(ApiCost.cost_mxn), 0),
            func.count(ApiCost.id),
        ).filter(ApiCost.created_at >= month_start).first()
    )
    chatbot_today = (
        db.session.query(func.count(ApiCost.id))
        .filter(ApiCost.service == "claude_api", ApiCost.action == "chatbot_message")
        .filter(ApiCost.created_at >= today_start).scalar() or 0
    )
    chatbot_month = (
        db.session.query(
            func.count(ApiCost.id),
            func.coalesce(func.sum(ApiCost.cost_mxn), 0),
        ).filter(ApiCost.service == "claude_api", ApiCost.action == "chatbot_message")
        .filter(ApiCost.created_at >= month_start).first()
    )
    cb_count = int(chatbot_month[0] or 0) if chatbot_month else 0
    cb_mxn = float(chatbot_month[1] or 0) if chatbot_month else 0
    avg_per_convo_mxn = (cb_mxn / cb_count) if cb_count else 0

    return {
        "total_usd": float(total[0] or 0),
        "total_mxn": float(total[1] or 0),
        "total_count": int(total[2] or 0),
        "byService": [
            {"service": s, "total_usd": float(usd or 0), "total_mxn": float(mxn or 0), "count": int(c)}
            for s, usd, mxn, c in by_service
        ],
        "byUnit": [
            {"unit": u, "total_usd": float(usd or 0), "total_mxn": float(mxn or 0), "count": int(c)}
            for u, usd, mxn, c in by_unit
        ],
        "byDay": [
            {"date": d.isoformat() if hasattr(d, "isoformat") else str(d),
             "total_usd": float(usd or 0), "total_mxn": float(mxn or 0), "count": int(c)}
            for d, usd, mxn, c in by_day
        ],
        "todayMxn": float(today_stats[0] or 0),
        "monthMxn": float(month_stats[0] or 0),
        "chatbotToday": int(chatbot_today),
        "avgPerConvoMxn": avg_per_convo_mxn,
    }


def get_cost_detail(service: Optional[str] = None, unit: Optional[str] = None,
                    page: int = 1, limit: int = 50) -> list:
    q = ApiCost.query
    if service:
        q = q.filter(ApiCost.service == service)
    if unit:
        q = q.filter(ApiCost.unit == unit)
    page = max(1, page)
    limit = max(1, min(limit, 500))
    rows = (
        q.order_by(ApiCost.created_at.desc())
        .offset((page - 1) * limit).limit(limit).all()
    )
    return [r.to_dict() for r in rows]
