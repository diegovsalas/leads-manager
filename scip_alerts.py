"""
SCIP Alerts handler. Port focal de scip/alerts-handler.js (~120 líneas).

Detecta anomalías en campañas activas (CTR bajo, frequency alta, sin
conversiones tras spend significativo, CPC alto). Devuelve lista de alerts;
el caller decide si los persiste, los notifica via webhook, o los muestra.

Diseñado para correr como cron diario (apscheduler) o on-demand desde la UI.
"""
import logging
from typing import Optional

import scip_meta
import scip_director
import scip_google

log = logging.getLogger("scip_alerts")


# Umbrales por defecto (idénticos al legacy + ajustables vía override)
DEFAULT_THRESHOLDS = {
    "ctr_min_pct": 0.5,           # < 0.5% es alerta
    "frequency_max": 5.0,         # > 5 indica fatiga
    "cpc_max_mxn": 30.0,          # CPC > $30 MXN
    "spend_no_conv_min": 1000.0,  # spend > $1000 sin conversiones es crítico
}


def evaluate_campaign(campaign: dict, thresholds: Optional[dict] = None) -> list:
    """Evalúa 1 campaña normalizada (formato fetch_meta/google_campaigns).
    Devuelve lista de alerts con shape: {level, type, campaign_id, campaign_name, message}."""
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    alerts = []
    if campaign.get("status") not in ("ACTIVE",):
        return alerts
    metrics = campaign.get("metrics") or {}
    cid = campaign.get("id")
    cname = campaign.get("name")
    platform = campaign.get("platform")
    unit = campaign.get("unit")

    def _alert(level, type_, msg):
        return {
            "level": level, "type": type_,
            "campaign_id": cid, "campaign_name": cname,
            "platform": platform, "unit": unit,
            "message": msg,
            "metrics_snapshot": {
                "spend": metrics.get("spend", 0),
                "ctr": metrics.get("ctr", 0),
                "cpc": metrics.get("cpc", 0),
                "frequency": metrics.get("frequency", 0),
                "conversations": metrics.get("conversations", 0),
                "purchases": metrics.get("purchases", 0),
            },
        }

    ctr = metrics.get("ctr", 0)
    if ctr < th["ctr_min_pct"]:
        alerts.append(_alert("warning", "low_ctr",
            f"CTR muy bajo: {ctr:.2f}% (umbral {th['ctr_min_pct']}%)"))

    freq = metrics.get("frequency", 0)
    if freq > th["frequency_max"]:
        alerts.append(_alert("warning", "high_frequency",
            f"Frequency alta: {freq:.1f} (umbral {th['frequency_max']}) — posible fatiga creativa"))

    cpc = metrics.get("cpc", 0)
    if cpc > th["cpc_max_mxn"]:
        alerts.append(_alert("warning", "high_cpc",
            f"CPC alto: ${cpc:.2f} (umbral ${th['cpc_max_mxn']})"))

    spend = metrics.get("spend", 0)
    convs = (metrics.get("conversations", 0) +
             metrics.get("purchases", 0) +
             metrics.get("conversions", 0))
    if spend > th["spend_no_conv_min"] and convs == 0:
        alerts.append(_alert("critical", "no_conversions",
            f"Spend ${spend:.2f} sin conversiones — revisar creatividad/audiencia"))

    return alerts


def scan_all(platform: Optional[str] = None,
              unit_filter: Optional[str] = None,
              thresholds: Optional[dict] = None) -> dict:
    """Escanea TODAS las campañas activas (Meta + Google) y devuelve
    el conjunto de alerts agrupado."""
    payload = scip_director.list_eligible_campaigns(platform=platform, unit_filter=unit_filter)
    campaigns = payload.get("data") or []
    all_alerts = []
    by_severity = {"critical": 0, "warning": 0, "info": 0}
    for c in campaigns:
        for alert in evaluate_campaign(c, thresholds):
            all_alerts.append(alert)
            by_severity[alert["level"]] = by_severity.get(alert["level"], 0) + 1
    return {
        "scanned_at": payload.get("timestamp"),
        "campaigns_scanned": len(campaigns),
        "total_alerts": len(all_alerts),
        "by_severity": by_severity,
        "alerts": all_alerts,
    }
