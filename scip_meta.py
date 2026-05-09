"""
SCIP Meta Ads service. Port directo de scip/meta-ads.service.js (845 líneas).

Sin SDK — la API Graph se consume vía requests, igual que el legacy con axios.
Cache en memoria (TTL 5min). Para multi-worker producción usar Redis; ahora
basta porque gevent share state.

Cuentas configuradas via env vars:
  META_ACCESS_TOKEN, META_APP_ID, META_API_VERSION (default v19.0)
  META_ACCOUNT_B2C, META_ACCOUNT_B2B, META_ACCOUNT_WELDU
"""
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

import requests

log = logging.getLogger("scip_meta")

API_VERSION = os.getenv("META_API_VERSION", "v19.0")
BASE_URL = os.getenv("META_BASE_URL", "https://graph.facebook.com")
ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
APP_ID = os.getenv("META_APP_ID", "")

ACCOUNTS = {
    "aromatex_b2c": os.getenv("META_ACCOUNT_B2C", ""),
    "aromatex_b2b": os.getenv("META_ACCOUNT_B2B", ""),
    "weldu":        os.getenv("META_ACCOUNT_WELDU", ""),
}

CACHE_TTL = 300  # 5 min
RATE_LIMIT_DELAY = 0.5  # seconds between calls
RETRY_ATTEMPTS = 3

# ── Cache simple en memoria ────────────────────────────────────────


class _Cache:
    def __init__(self, ttl: int = CACHE_TTL):
        self._d: dict = {}
        self._ttl = ttl

    def get(self, key: str):
        item = self._d.get(key)
        if not item:
            return None
        value, expires = item
        if time.time() > expires:
            del self._d[key]
            return None
        return value

    def set(self, key: str, value, ttl: Optional[int] = None):
        self._d[key] = (value, time.time() + (ttl or self._ttl))

    def flush(self):
        self._d.clear()


_cache = _Cache()


# ── HTTP helpers ───────────────────────────────────────────────────


def is_configured() -> bool:
    return bool(ACCESS_TOKEN)


def _build_url(endpoint: str, params: Optional[dict] = None) -> tuple[str, dict]:
    url = f"{BASE_URL}/{API_VERSION}/{endpoint}"
    qp = {"access_token": ACCESS_TOKEN, **(params or {})}
    return url, qp


def _make_request(endpoint: str, params: Optional[dict] = None,
                   method: str = "GET", retries: int = RETRY_ATTEMPTS) -> dict:
    if not is_configured():
        raise RuntimeError("META_ACCESS_TOKEN no configurado")
    url, qp = _build_url(endpoint, params)
    last_err = None
    for attempt in range(retries):
        try:
            log.debug(f"[META] {method} {endpoint}")
            resp = requests.request(method, url, params=qp, timeout=15)
            if resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                time.sleep(2 ** attempt)
                continue
            data = resp.json() if resp.content else {}
            if data.get("error"):
                err = data["error"]
                if err.get("code") in (4, 17, 32, 613):  # rate limit
                    log.warning(f"[META] Rate limit hit, sleeping 60s")
                    time.sleep(60)
                    continue
                raise RuntimeError(f"Meta API error: {err.get('message')}")
            return data
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Meta API failed after {retries} retries: {last_err}")


def _default_date_range() -> tuple[str, str]:
    until = date.today()
    since = until - timedelta(days=30)
    return since.isoformat(), until.isoformat()


# ── Parsers ────────────────────────────────────────────────────────


def parse_metrics(metrics_data: list) -> dict:
    """Toma el array data de /insights y extrae KPIs agregados.
    Replica parseMetrics() del legacy."""
    if not metrics_data:
        return _empty_metrics()
    agg = {
        "impressions": 0, "clicks": 0, "spend": 0.0, "reach": 0,
        "cpc": 0.0, "cpm": 0.0, "cpp": 0.0, "ctr": 0.0, "frequency": 0.0,
        "actions": {}, "action_values": {},
        "conversations": 0, "purchases": 0, "downloads": 0, "leads": 0,
    }
    for entry in metrics_data:
        agg["impressions"] += int(entry.get("impressions") or 0)
        agg["clicks"] += int(entry.get("clicks") or 0)
        agg["spend"] += float(entry.get("spend") or 0)
        agg["reach"] += int(entry.get("reach") or 0)
        for f in ("cpc", "cpm", "cpp", "ctr", "frequency"):
            v = entry.get(f)
            if v is not None:
                try:
                    agg[f] = float(v)
                except (ValueError, TypeError):
                    pass
        for action in (entry.get("actions") or []):
            atype = action.get("action_type") or "unknown"
            agg["actions"][atype] = agg["actions"].get(atype, 0) + int(float(action.get("value") or 0))
            if "messaging_conversation" in atype.lower() or atype == "onsite_conversion.messaging_first_reply":
                agg["conversations"] += int(float(action.get("value") or 0))
            elif atype in ("purchase", "omni_purchase", "offsite_conversion.fb_pixel_purchase"):
                agg["purchases"] += int(float(action.get("value") or 0))
            elif atype in ("mobile_app_install", "app_install"):
                agg["downloads"] += int(float(action.get("value") or 0))
            elif atype in ("lead", "leadgen.other"):
                agg["leads"] += int(float(action.get("value") or 0))
        for av in (entry.get("action_values") or []):
            atype = av.get("action_type") or "unknown"
            agg["action_values"][atype] = agg["action_values"].get(atype, 0.0) + float(av.get("value") or 0)
    return agg


def _empty_metrics() -> dict:
    return {
        "impressions": 0, "clicks": 0, "spend": 0.0, "reach": 0,
        "cpc": 0.0, "cpm": 0.0, "cpp": 0.0, "ctr": 0.0, "frequency": 0.0,
        "actions": {}, "action_values": {},
        "conversations": 0, "purchases": 0, "downloads": 0, "leads": 0,
    }


# ── Public API ─────────────────────────────────────────────────────


def get_campaigns(account_name: str = "aromatex_b2c") -> list:
    cache_key = f"campaigns_{account_name}"
    cached = _cache.get(cache_key)
    if cached:
        return cached

    account_id = ACCOUNTS.get(account_name)
    if not account_id:
        raise RuntimeError(f"Cuenta desconocida: {account_name}")

    fields = "id,name,status,objective,daily_budget,lifetime_budget,created_time,updated_time,start_time,stop_time,bid_strategy,buying_type"
    data = _make_request(f"{account_id}/campaigns", {"fields": fields, "limit": 100})

    campaigns = []
    for c in (data.get("data") or []):
        try:
            metrics = get_campaign_metrics(c["id"])
        except Exception as e:
            metrics = _empty_metrics()
            log.warning(f"metrics for campaign {c['id']} failed: {e}")
        campaigns.append({**c, "metrics": metrics})

    _cache.set(cache_key, campaigns)
    return campaigns


def get_adsets_by_campaign(campaign_id: str) -> list:
    cache_key = f"adsets_{campaign_id}"
    cached = _cache.get(cache_key)
    if cached:
        return cached

    fields = "id,name,campaign_id,status,daily_budget,targeting,bid_strategy,bid_amount,optimization_goal,created_time,updated_time"
    data = _make_request(f"{campaign_id}/adsets", {"fields": fields, "limit": 100})

    adsets = []
    for a in (data.get("data") or []):
        try:
            metrics = get_adset_metrics(a["id"])
        except Exception:
            metrics = _empty_metrics()
        adsets.append({**a, "metrics": metrics})

    _cache.set(cache_key, adsets)
    return adsets


def get_ads_by_adset(adset_id: str) -> list:
    cache_key = f"ads_{adset_id}"
    cached = _cache.get(cache_key)
    if cached:
        return cached

    fields = "id,name,adset_id,status,creative,created_time,updated_time"
    data = _make_request(f"{adset_id}/ads", {"fields": fields, "limit": 100})

    ads = []
    for ad in (data.get("data") or []):
        try:
            metrics = get_ad_metrics(ad["id"])
        except Exception:
            metrics = _empty_metrics()
        creative_id = (ad.get("creative") or {}).get("id")
        creative = get_creative_details(creative_id) if creative_id else {}
        ads.append({**ad, "metrics": metrics, "creative_details": creative})

    _cache.set(cache_key, ads)
    return ads


def _get_metrics(entity_id: str, kind: str, date_range: Optional[tuple] = None) -> dict:
    cache_key = f"metrics_{kind}_{entity_id}"
    cached = _cache.get(cache_key)
    if cached:
        return cached
    since, until = date_range or _default_date_range()
    fields = "impressions,clicks,spend,actions,action_values,cpc,cpm,frequency,ctr,reach"
    import json
    data = _make_request(f"{entity_id}/insights", {
        "fields": fields,
        "time_range": json.dumps({"since": since, "until": until}),
        "action_breakdown": "action_type",
    })
    metrics = parse_metrics(data.get("data") or [])
    _cache.set(cache_key, metrics)
    return metrics


def get_campaign_metrics(campaign_id: str, date_range=None) -> dict:
    return _get_metrics(campaign_id, "campaign", date_range)


def get_adset_metrics(adset_id: str, date_range=None) -> dict:
    return _get_metrics(adset_id, "adset", date_range)


def get_ad_metrics(ad_id: str, date_range=None) -> dict:
    return _get_metrics(ad_id, "ad", date_range)


def get_creative_details(creative_id: Optional[str]) -> dict:
    if not creative_id:
        return {}
    cache_key = f"creative_{creative_id}"
    cached = _cache.get(cache_key)
    if cached:
        return cached
    try:
        data = _make_request(f"{creative_id}", {
            "fields": "id,name,title,body,image_url,thumbnail_url,object_type,call_to_action_type",
        })
        _cache.set(cache_key, data, ttl=3600)  # creative details rarely change
        return data
    except Exception:
        return {}


def get_account_daily_insights(account_name: str, since: str, until: str) -> list:
    """Granularidad diaria. Para gráfico de tendencias."""
    account_id = ACCOUNTS.get(account_name)
    if not account_id:
        raise RuntimeError(f"Cuenta desconocida: {account_name}")
    cache_key = f"daily_{account_name}_{since}_{until}"
    cached = _cache.get(cache_key)
    if cached:
        return cached
    import json
    data = _make_request(f"{account_id}/insights", {
        "time_increment": 1,
        "fields": "spend,impressions,clicks,cpc,cpm,ctr,frequency,actions,date_start",
        "time_range": json.dumps({"since": since, "until": until}),
        "action_breakdowns": "action_type",
        "limit": 200,
    })

    def _find(actions, predicate):
        for a in actions or []:
            if predicate(a):
                return float(a.get("value") or 0)
        return 0

    daily = []
    for d in (data.get("data") or []):
        actions = d.get("actions") or []
        daily.append({
            "date": d.get("date_start"),
            "spend": float(d.get("spend") or 0),
            "impressions": int(d.get("impressions") or 0),
            "clicks": int(d.get("clicks") or 0),
            "cpc": float(d.get("cpc") or 0),
            "cpm": float(d.get("cpm") or 0),
            "ctr": float(d.get("ctr") or 0),
            "frequency": float(d.get("frequency") or 0),
            "conversations": _find(actions, lambda a: "messaging_conversation" in (a.get("action_type") or "").lower() or a.get("action_type") == "onsite_conversion.messaging_first_reply"),
            "purchases": _find(actions, lambda a: a.get("action_type") in ("purchase", "omni_purchase", "offsite_conversion.fb_pixel_purchase")),
            "downloads": _find(actions, lambda a: a.get("action_type") in ("mobile_app_install", "app_install")),
        })
    _cache.set(cache_key, daily)
    return daily


# ── High-level reports ────────────────────────────────────────────


def get_full_sync(account_name: str = "aromatex_b2c") -> dict:
    """Pull completo: cuenta + todas las campañas + adsets + ads.
    Costo: muchas llamadas API. Usar con moderación. Cache 5min cubre."""
    campaigns = get_campaigns(account_name)
    full = []
    for c in campaigns:
        adsets = get_adsets_by_campaign(c["id"])
        for a in adsets:
            a["ads"] = get_ads_by_adset(a["id"])
        full.append({**c, "adsets": adsets})
    return {
        "account": account_name,
        "synced_at": datetime.utcnow().isoformat(),
        "campaigns_count": len(full),
        "campaigns": full,
    }


def generate_alerts(campaign: dict, metrics: dict) -> list:
    """Detecta anomalías en una campaña. Retorna lista de alerts."""
    alerts = []
    if campaign.get("status") == "ACTIVE":
        if metrics.get("ctr", 0) < 0.5:
            alerts.append({
                "level": "warning", "type": "low_ctr",
                "message": f"CTR muy bajo: {metrics['ctr']:.2f}% (umbral 0.5%)",
            })
        if metrics.get("frequency", 0) > 5:
            alerts.append({
                "level": "warning", "type": "high_frequency",
                "message": f"Frequency alta: {metrics['frequency']:.1f} (umbral 5)",
            })
        if metrics.get("spend", 0) > 0 and metrics.get("conversations", 0) == 0 and metrics.get("purchases", 0) == 0:
            alerts.append({
                "level": "critical", "type": "no_conversions",
                "message": f"Spend ${metrics['spend']:.2f} sin conversiones",
            })
        cpc = metrics.get("cpc", 0)
        if cpc > 30:
            alerts.append({
                "level": "warning", "type": "high_cpc",
                "message": f"CPC alto: ${cpc:.2f}",
            })
    return alerts


def generate_recommendations(campaign: dict, metrics: dict) -> list:
    """Sugerencias de acción basadas en performance."""
    recs = []
    spend = metrics.get("spend", 0)
    convos = metrics.get("conversations", 0)
    if campaign.get("status") == "ACTIVE":
        if spend > 1000 and convos == 0:
            recs.append({
                "action": "pause", "reason": "Sin conversiones tras spend significativo",
                "priority": "high",
            })
        elif convos > 10 and metrics.get("cpc", 999) < 5:
            recs.append({
                "action": "scale_up", "reason": f"{convos} conversaciones a CPC bajo — escalar +20% budget",
                "priority": "high",
            })
        if metrics.get("frequency", 0) > 4:
            recs.append({
                "action": "refresh_creative", "reason": "Frequency alta indica fatiga creativa",
                "priority": "medium",
            })
    return recs


def get_marketinsito_report(account_name: str = "aromatex_b2c") -> dict:
    """Reporte ejecutivo: campañas + alerts + recommendations + summary."""
    campaigns = get_campaigns(account_name)
    summary = {
        "total_campaigns": len(campaigns),
        "active": sum(1 for c in campaigns if c.get("status") == "ACTIVE"),
        "paused": sum(1 for c in campaigns if c.get("status") == "PAUSED"),
        "total_spend": sum(c.get("metrics", {}).get("spend", 0) for c in campaigns),
        "total_conversations": sum(c.get("metrics", {}).get("conversations", 0) for c in campaigns),
        "total_purchases": sum(c.get("metrics", {}).get("purchases", 0) for c in campaigns),
    }
    alerts_all = []
    recs_all = []
    for c in campaigns:
        m = c.get("metrics") or {}
        for a in generate_alerts(c, m):
            alerts_all.append({**a, "campaign_id": c["id"], "campaign_name": c.get("name")})
        for r in generate_recommendations(c, m):
            recs_all.append({**r, "campaign_id": c["id"], "campaign_name": c.get("name")})
    return {
        "account": account_name,
        "generated_at": datetime.utcnow().isoformat(),
        "summary": summary,
        "campaigns": campaigns,
        "alerts": alerts_all,
        "recommendations": recs_all,
    }


def get_creative_performance(account_name: str = "aromatex_b2c") -> list:
    """Top creativos por performance. Útil para SCIP director: qué ad escalar."""
    campaigns = get_campaigns(account_name)
    creatives = []
    for c in campaigns:
        if c.get("status") != "ACTIVE":
            continue
        try:
            adsets = get_adsets_by_campaign(c["id"])
        except Exception:
            continue
        for adset in adsets:
            try:
                ads = get_ads_by_adset(adset["id"])
            except Exception:
                continue
            for ad in ads:
                m = ad.get("metrics") or {}
                creatives.append({
                    "campaign_id": c["id"], "campaign_name": c.get("name"),
                    "adset_id": adset["id"], "adset_name": adset.get("name"),
                    "ad_id": ad["id"], "ad_name": ad.get("name"),
                    "creative": ad.get("creative_details") or {},
                    "metrics": m,
                    "spend": m.get("spend", 0), "ctr": m.get("ctr", 0),
                    "conversations": m.get("conversations", 0),
                })
    creatives.sort(key=lambda x: -(x["spend"] or 0))
    return creatives[:50]


def flush_cache() -> dict:
    n = len(_cache._d)
    _cache.flush()
    return {"flushed": n}


def health() -> dict:
    return {
        "configured": is_configured(),
        "api_version": API_VERSION,
        "accounts": {k: bool(v) for k, v in ACCOUNTS.items()},
        "cached_keys": len(_cache._d),
    }
