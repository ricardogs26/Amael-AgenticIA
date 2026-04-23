"""scheduler.py debe invocar el dispatcher camael_client, no healer directo."""
from __future__ import annotations


def test_scheduler_imports_camael_client_dispatcher(monkeypatch):
    """scheduler.py usa camael_client.handoff_to_camael (no healer.handoff_to_camael)."""
    import importlib
    import agents.sre.scheduler as sched_mod
    source = open(sched_mod.__file__).read()

    # Debe haber al menos una referencia al dispatcher
    assert (
        "camael_client.handoff_to_camael" in source
        or "from clients.camael_client import handoff_to_camael" in source
    ), "scheduler.py debería invocar clients.camael_client.handoff_to_camael"

    # Y NO debe tener el call site directo legacy
    assert "healer.handoff_to_camael(" not in source, (
        "scheduler.py ya NO debe llamar healer.handoff_to_camael directamente "
        "— debe pasar por el dispatcher camael_client"
    )
