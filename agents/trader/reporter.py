"""
reporter.py — Notificaciones WhatsApp del trader (mismo patrón que agents.sre.reporter).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("agents.trader.reporter")

_WHATSAPP_BRIDGE_URL = os.environ.get("WHATSAPP_BRIDGE_URL", "http://whatsapp-bridge-service:3000")
_OWNER_PHONE = os.environ.get("TRADER_ALERT_PHONE", os.environ.get("SRE_ALERT_PHONE", ""))


def notify_whatsapp(message: str) -> bool:
    if not _OWNER_PHONE or not _WHATSAPP_BRIDGE_URL:
        logger.warning("[reporter] WhatsApp no configurado (TRADER_ALERT_PHONE / WHATSAPP_BRIDGE_URL)")
        return False
    try:
        import requests
        resp = requests.post(
            f"{_WHATSAPP_BRIDGE_URL}/send",
            json={"phoneNumber": _OWNER_PHONE, "text": message},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("[reporter] Notificación WhatsApp enviada")
            return True
        logger.warning(f"[reporter] WhatsApp error {resp.status_code}: {resp.text[:100]}")
    except Exception as exc:
        logger.error(f"[reporter] notify_whatsapp error: {exc}")
    return False


def daily_report() -> None:
    """Reporte al cierre del día (programado por loop.py tras el cierre NYSE)."""
    from agents.trader.broker import TRADER_MODE, get_account_snapshot
    from agents.trader.storage import get_equity_curve, get_recent_orders

    try:
        acct = get_account_snapshot()
        orders = [o for o in get_recent_orders(20) if o["status"] == "executed"]
        curve = get_equity_curve(2)
        prev = float(curve[1]["equity_usd"]) if len(curve) > 1 else acct.equity_usd
        delta = acct.equity_usd - prev

        lines = [
            f"📊 *Trader ({TRADER_MODE})* — reporte diario",
            f"Equity: ${acct.equity_usd:,.2f} ({'+' if delta >= 0 else ''}{delta:,.2f})",
            f"Cash: ${acct.cash_usd:,.2f}",
        ]
        if acct.positions:
            lines.append("Posiciones:")
            for p in acct.positions:
                lines.append(f"  • {p['symbol']}: ${p['market_value']:,.2f} (P&L {p['unrealized_pl']:+,.2f})")
        else:
            lines.append("Sin posiciones abiertas.")
        if orders:
            lines.append("Órdenes recientes ejecutadas:")
            for o in orders[:5]:
                lines.append(f"  • {o['action']} {o['symbol']} ${float(o['notional_usd']):.2f}")
        notify_whatsapp("\n".join(lines))
    except Exception as exc:
        logger.error(f"[reporter] daily_report error: {exc}", exc_info=True)
