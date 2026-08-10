"""
Regresión del 10-ago-2026: OOM re-reportado como lápida.

`container_status.last_state.terminated.OOMKilled` persiste para SIEMPRE en un
pod que hizo OOM una vez, aunque lleve horas Running. El observer lo trataba
como incidente nuevo cada ciclo: el frontend murió a las 08:00, se recuperó, y
Raphael notificó el MISMO OOM 7 veces (hasta las 14:03), intentando
ROLLOUT_RESTART inútil y mandando WhatsApp cada vez.

`_oom_still_relevant` corta eso: un OOM cuyo pod ya lleva rato sano está
resuelto y no se reporta.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

from agents.sre.observer import _oom_still_relevant


def _cs(running_since_s: float | None, oom: bool = True):
    """Container status con OOM en last_state y un state configurable."""
    last = NS(terminated=NS(reason="OOMKilled")) if oom else NS(terminated=None)
    if running_since_s is None:
        # No está corriendo — waiting (backoff) o terminated actual.
        return NS(state=NS(running=None, waiting=NS(reason="CrashLoopBackOff")),
                  last_state=last, restart_count=1)
    started = datetime.now(UTC) - timedelta(seconds=running_since_s)
    return NS(state=NS(running=NS(started_at=started), waiting=None),
              last_state=last, restart_count=1)


def test_oom_de_pod_sano_hace_horas_no_es_relevante():
    """El caso exacto del incidente: pod Running desde hace 6 h."""
    assert _oom_still_relevant(_cs(running_since_s=6 * 3600)) is False


def test_oom_de_pod_recien_reiniciado_si_es_relevante():
    """Acaba de reiniciar: el reinicio aún no demuestra estabilidad."""
    assert _oom_still_relevant(_cs(running_since_s=30)) is True


def test_oom_de_pod_que_no_corre_si_es_relevante():
    """waiting/CrashLoop con OOM previo = el OOM está activo ahora."""
    assert _oom_still_relevant(_cs(running_since_s=None)) is True


def test_frontera_de_la_ventana():
    import agents.sre.observer as obs
    limite = obs._OOM_RESOLVED_AFTER_S
    assert _oom_still_relevant(_cs(running_since_s=limite - 5)) is True
    assert _oom_still_relevant(_cs(running_since_s=limite + 5)) is False


def test_started_at_ausente_no_truena():
    cs = NS(state=NS(running=NS(started_at=None), waiting=None),
            last_state=NS(terminated=NS(reason="OOMKilled")), restart_count=1)
    assert _oom_still_relevant(cs) is True   # ante la duda, reportar
