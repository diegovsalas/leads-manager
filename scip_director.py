"""
SCIP Director — orquestación de análisis y decisiones sobre campañas.

Port focalizado de scip/director.routes.js (587 líneas legacy).
Junta Meta + Google en una sola lista normalizada, llama a Claude para
sugerir acción, y persiste recomendaciones en ScipDirectorRecommendation.

Constantes de negocio (DAYS_LEARNING_PHASE=7) y mapeos cuenta→unidad
preservados del legacy.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from extensions import db
from models import Lead, EtapaPipeline, ScipDirectorRecommendation
import scip_meta
import scip_google

log = logging.getLogger("scip_director")

DAYS_LEARNING_PHASE = 7
DIRECTOR_MODEL = "claude-sonnet-4-20250514"
DIRECTOR_MAX_TOKENS = 1500
CLAUDE_PRICE_IN_PER_MTOK = 3.0
CLAUDE_PRICE_OUT_PER_MTOK = 15.0

META_ACCOUNTS = ["aromatex_b2c", "aromatex_b2b"]  # weldu excluido (spec legacy)
META_UNIT_BY_ACCOUNT = {"aromatex_b2c": "aromatex_b2c", "aromatex_b2b": "aromatex_b2b"}

GOOGLE_CUSTOMER_IDS = {
    "aromatex_business": "4237414897",
    "aromatex_home":     "3725611345",
    "pestex_business":   "3075736100",
    "weldex":            "3419420947",
}
GOOGLE_UNIT_BY_KEY = {
    "aromatex_business": "aromatex",
    "aromatex_home":     "aromatex",
    "pestex_business":   "pestex",
    "weldex":            "weldex",
}

# Cache simple para /campaigns (la respuesta combinada Meta+Google es cara)
_director_cache: dict = {}
_DIRECTOR_CACHE_TTL = 120  # 2 min


def _cache_get(key: str):
    item = _director_cache.get(key)
    if not item:
        return None
    val, exp = item
    if time.time() > exp:
        _director_cache.pop(key, None)
        return None
    return val


def _cache_set(key: str, val):
    _director_cache[key] = (val, time.time() + _DIRECTOR_CACHE_TTL)


def _days_since(iso_date: Optional[str]) -> int:
    if not iso_date:
        return 0
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        return max(0, delta.days)
    except (ValueError, TypeError):
        return 0


def _safe_num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


# ── Fetchers normalizados ──────────────────────────────────────────


def fetch_meta_campaigns(account: str) -> list:
    """Wrap scip_meta.get_campaigns con shape uniforme: id/name/platform/
    account/unit/status/days_running/eligible_for_analysis/budget/metrics."""
    try:
        raw = scip_meta.get_campaigns(account)
    except Exception as e:
        log.warning(f"[DIR] meta {account}: {e}")
        return []
    out = []
    for c in raw:
        if c.get("status") not in ("ACTIVE", "PAUSED"):
            continue
        m = c.get("metrics") or {}
        start = c.get("start_time") or c.get("created_time")
        days = _days_since(start)
        lifetime = _safe_num(c.get("lifetime_budget"))
        daily = _safe_num(c.get("daily_budget"))
        budget_total = (lifetime / 100) if lifetime > 0 else (
            (daily / 100) * max(days, 1) if daily > 0 else None
        )
        eligible = days >= DAYS_LEARNING_PHASE
        available_after = None
        if not eligible and start:
            try:
                base = datetime.fromisoformat(start.replace("Z", "+00:00"))
                from datetime import timedelta
                available_after = (base + timedelta(days=DAYS_LEARNING_PHASE)).date().isoformat()
            except (ValueError, TypeError):
                pass
        out.append({
            "id": c.get("id"), "name": c.get("name"), "platform": "meta",
            "account": account,
            "unit": META_UNIT_BY_ACCOUNT.get(account, account),
            "status": c.get("status"),
            "start_time": start, "days_running": days,
            "eligible_for_analysis": eligible,
            "available_after": available_after,
            "budget": {
                "total": budget_total,
                "daily": (daily / 100) if daily > 0 else None,
                "consumed": m.get("spend", 0),
            },
            "metrics": m,
        })
    return out


def fetch_google_campaigns(account_key: str) -> list:
    """Wrap scip_google.get_campaigns con shape uniforme."""
    customer_id = GOOGLE_CUSTOMER_IDS.get(account_key)
    if not customer_id:
        return []
    try:
        result = scip_google.get_campaigns(customer_id)
    except Exception as e:
        log.warning(f"[DIR] google {account_key}: {e}")
        return []
    if not result.get("success"):
        return []
    out = []
    for c in result["data"]:
        if c.get("status") not in ("ENABLED", "PAUSED"):
            continue
        m = c.get("metrics") or {}
        # Google no expone created_time en este query — usamos days_running fijo a LAST_30_DAYS proxy
        days = 30
        budget_total = c.get("budget_amount_mxn")
        out.append({
            "id": c.get("id"), "name": c.get("name"), "platform": "google",
            "account": account_key,
            "unit": GOOGLE_UNIT_BY_KEY.get(account_key, account_key),
            "status": "ACTIVE" if c.get("status") == "ENABLED" else c.get("status"),
            "start_time": None, "days_running": days,
            "eligible_for_analysis": days >= DAYS_LEARNING_PHASE,
            "available_after": None,
            "budget": {
                "total": budget_total, "daily": None,
                "consumed": m.get("spend", 0),
            },
            "metrics": {
                "impressions": m.get("impressions", 0),
                "clicks": m.get("clicks", 0),
                "spend": m.get("spend", 0),
                "ctr": m.get("ctr", 0),
                "cpc": m.get("cpc", 0),
                "conversions": m.get("conversions", 0),
                "conversations": 0,  # Google no tiene este concepto
            },
        })
    return out


def count_leads_for_unit(unit: str) -> dict:
    """Lead counts internos para enriquecer la lista. Best-effort: matchea
    Lead.marca_interes case-insensitive contra el unit."""
    try:
        u = (unit or "").lower()
        # Mapeo simple a Marca Aromatex/Pestex/Weldex
        marca = None
        if "aromatex" in u:
            marca = "Aromatex"
        elif "pestex" in u:
            marca = "Pestex"
        elif "weldex" in u or "weldu" in u:
            marca = "Weldex"
        if not marca:
            return {"total": 0, "ganados": 0, "en_proceso": 0}
        base = Lead.query.filter(Lead.marca_interes == marca)
        return {
            "total": base.count(),
            "ganados": base.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO).count(),
            "en_proceso": base.filter(
                Lead.etapa_pipeline.notin_([EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO])
            ).count(),
        }
    except Exception:
        return {"total": 0, "ganados": 0, "en_proceso": 0}


def list_eligible_campaigns(platform: Optional[str] = None,
                             unit_filter: Optional[str] = None,
                             min_days: int = DAYS_LEARNING_PHASE) -> dict:
    cache_key = f"director:{platform or 'all'}:{unit_filter or 'all'}:{min_days}"
    cached = _cache_get(cache_key)
    if cached:
        return {"cached": True, **cached}

    campaigns = []
    if not platform or platform == "meta":
        for acc in META_ACCOUNTS:
            campaigns.extend(fetch_meta_campaigns(acc))
    if not platform or platform == "google":
        for key in GOOGLE_CUSTOMER_IDS:
            campaigns.extend(fetch_google_campaigns(key))

    if unit_filter:
        u = unit_filter.lower()
        campaigns = [c for c in campaigns if u in (c.get("unit") or "").lower()]

    # Enrich lead counts
    by_unit: dict = {}
    for c in campaigns:
        if c["unit"] not in by_unit:
            by_unit[c["unit"]] = count_leads_for_unit(c["unit"])
    for c in campaigns:
        c["leads_estimate"] = by_unit.get(c["unit"])

    # Order: eligible primero, luego days_running desc
    campaigns.sort(key=lambda c: (
        0 if c.get("eligible_for_analysis") else 1,
        -(c.get("days_running") or 0),
    ))

    payload = {
        "cached": False,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "count": len(campaigns),
        "eligible_count": sum(1 for c in campaigns if c.get("eligible_for_analysis")),
        "data": campaigns,
    }
    _cache_set(cache_key, payload)
    return payload


# ── Anthropic analysis ─────────────────────────────────────────────


def _build_director_prompt(unit: str, metrics: dict, leads_closed: int,
                            leads_in_pipeline: int, days_running: int,
                            budget_total: Optional[float],
                            budget_consumed: float) -> str:
    return f"""Eres Director Comercial de Grupo Avantex analizando una campaña publicitaria.
Unidad de negocio: {unit or 'desconocida'}
Días corriendo: {days_running}
Budget total: ${budget_total or 'N/A'} | Consumido: ${budget_consumed:.2f}

MÉTRICAS DE LA CAMPAÑA (últimos 30 días):
- Impresiones: {metrics.get('impressions', 0):,}
- Clicks: {metrics.get('clicks', 0):,}
- Spend: ${metrics.get('spend', 0):.2f}
- CTR: {metrics.get('ctr', 0):.2f}%
- CPC: ${metrics.get('cpc', 0):.2f}
- CPM: ${metrics.get('cpm', 0):.2f}
- Frequency: {metrics.get('frequency', 0):.1f}
- Conversaciones: {metrics.get('conversations', 0)}
- Compras: {metrics.get('purchases', 0)}

LEADS REPORTADOS POR EL DIRECTOR:
- Cerrados: {leads_closed}
- En pipeline: {leads_in_pipeline}

Analiza con criterio comercial y devuelve EXCLUSIVAMENTE un JSON con:
{{
  "suggested_action": "scale_up" | "pause" | "duplicate_to" | "refresh_creative" | "keep" | "redirect_budget",
  "rationale": "Explicación corta (max 2 oraciones) en español",
  "confidence": 0-100,
  "expected_impact": "qué pasaría si se aplica la acción",
  "risk_warnings": ["warning1", "warning2"],
  "options": [
    {{"action": "...", "rationale": "...", "priority": "high|medium|low"}},
    {{"action": "...", "rationale": "...", "priority": "high|medium|low"}}
  ]
}}"""


def _try_parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    import re
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except (ValueError, TypeError):
            pass
    return None


def analyze_campaign(campaign_id: str, platform: str, account: Optional[str] = None,
                      leads_closed: int = 0, leads_in_pipeline: int = 0,
                      unit: Optional[str] = None, name: Optional[str] = None,
                      metrics: Optional[dict] = None) -> dict:
    """Llama a Claude con el snapshot de la campaña y devuelve análisis."""
    try:
        from anthropic import Anthropic
    except ImportError:
        return {"error": "anthropic SDK no instalado"}

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY no configurada"}

    # Resolver datos faltantes desde la fuente
    snapshot_metrics = metrics
    days_running = 0
    budget_total = None
    budget_consumed = 0.0
    resolved_unit = unit
    resolved_name = name

    if not snapshot_metrics or not resolved_unit:
        if platform == "meta":
            source_list = fetch_meta_campaigns(account or "aromatex_b2c")
        else:
            source_list = fetch_google_campaigns(account or "aromatex_business")
        found = next((c for c in source_list if str(c["id"]) == str(campaign_id)), None)
        if found:
            snapshot_metrics = snapshot_metrics or found.get("metrics")
            resolved_name = resolved_name or found.get("name")
            resolved_unit = resolved_unit or found.get("unit")
            days_running = found.get("days_running") or 0
            budget = found.get("budget") or {}
            budget_total = budget.get("total")
            budget_consumed = budget.get("consumed") or 0

    snapshot_metrics = snapshot_metrics or {}
    snapshot = {
        "campaign": {
            "id": campaign_id, "name": resolved_name, "platform": platform,
            "unit": resolved_unit,
        },
        "days_running": days_running,
        "budget_total": budget_total, "budget_consumed": budget_consumed,
        "metrics": snapshot_metrics,
        "leads_closed_reported": leads_closed,
        "leads_in_pipeline_reported": leads_in_pipeline,
    }

    prompt = _build_director_prompt(
        resolved_unit, snapshot_metrics, leads_closed, leads_in_pipeline,
        days_running, budget_total, budget_consumed,
    )

    try:
        client = Anthropic(api_key=api_key)
        response = client.messages.create(
            model=DIRECTOR_MODEL, max_tokens=DIRECTOR_MAX_TOKENS,
            system=prompt,
            messages=[{
                "role": "user",
                "content": "Analiza esta campaña con criterio de Director Comercial. Responde solo el JSON pedido.",
            }],
        )
        raw = response.content[0].text if response.content else ""
        parsed = _try_parse_json(raw)

        in_tok = response.usage.input_tokens if response.usage else 0
        out_tok = response.usage.output_tokens if response.usage else 0
        cost_usd = (in_tok * CLAUDE_PRICE_IN_PER_MTOK + out_tok * CLAUDE_PRICE_OUT_PER_MTOK) / 1_000_000

        try:
            from api_costs import track_cost
            track_cost(
                service="claude_api", action="scip_director_analyze",
                unit=resolved_unit or "unknown",
                tokens_input=in_tok, tokens_output=out_tok,
                cost_usd=cost_usd,
                metadata={"campaign_id": campaign_id, "platform": platform},
            )
        except Exception:
            pass

        return {
            "campaign_id": campaign_id, "platform": platform,
            "analysis": parsed or {"raw": raw},
            "snapshot": snapshot,
            "cost_usd": round(cost_usd, 6),
            "tokens": {"in": in_tok, "out": out_tok},
        }
    except Exception as e:
        log.error(f"analyze_campaign: {e}")
        return {"error": str(e)}
