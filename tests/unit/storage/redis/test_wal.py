"""Unit tests for storage/redis/wal.py — Write-Ahead Log genérico."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def fake_redis(monkeypatch):
    """Redis en memoria con set/get/keys/delete + TTL mocked."""
    class FakeRedis:
        def __init__(self):
            self.db: dict[str, str] = {}
            self.ttls: dict[str, int] = {}

        def set(self, key, value, ex=None):
            self.db[key] = value
            if ex:
                self.ttls[key] = ex
            return True

        def get(self, key):
            return self.db.get(key)

        def keys(self, pattern):
            import fnmatch
            return [k for k in self.db if fnmatch.fnmatch(k, pattern)]

        def delete(self, *keys):
            for k in keys:
                self.db.pop(k, None)
                self.ttls.pop(k, None)
            return len(keys)

        def ttl(self, key):
            return self.ttls.get(key, -1)

    fr = FakeRedis()
    monkeypatch.setattr(
        "storage.redis.client.get_client",
        lambda: fr,
    )
    return fr


def test_enqueue_persists_payload_with_ttl(fake_redis):
    from storage.redis.wal import enqueue

    ok = enqueue(
        topic="handoff",
        key="incident-123",
        payload={"foo": "bar"},
        ttl_seconds=86400,
    )
    assert ok is True
    assert "wal:camael:handoff:incident-123" in fake_redis.db
    data = json.loads(fake_redis.db["wal:camael:handoff:incident-123"])
    assert data == {"foo": "bar"}
    assert fake_redis.ttls["wal:camael:handoff:incident-123"] == 86400


def test_enqueue_idempotent_same_key(fake_redis):
    """Mismo key sobrescribe sin error (idempotencia por key)."""
    from storage.redis.wal import enqueue

    enqueue("handoff", "inc-1", {"v": 1}, ttl_seconds=3600)
    enqueue("handoff", "inc-1", {"v": 2}, ttl_seconds=3600)

    keys = fake_redis.keys("wal:camael:handoff:*")
    assert len(keys) == 1
    assert json.loads(fake_redis.db[keys[0]])["v"] == 2


def test_drain_processes_all_pending_and_deletes_on_success(fake_redis):
    from storage.redis.wal import drain, enqueue

    enqueue("handoff", "inc-1", {"id": 1}, ttl_seconds=3600)
    enqueue("handoff", "inc-2", {"id": 2}, ttl_seconds=3600)

    processed = []

    def consumer(payload: dict) -> bool:
        processed.append(payload)
        return True  # éxito → drain debe DEL la key

    count = drain("handoff", consumer)

    assert count == 2
    assert len(processed) == 2
    assert fake_redis.keys("wal:camael:handoff:*") == []


def test_drain_keeps_entry_on_failure(fake_redis):
    from storage.redis.wal import drain, enqueue

    enqueue("handoff", "inc-fail", {"id": 1}, ttl_seconds=3600)

    def consumer(payload: dict) -> bool:
        return False  # fallo → drain NO borra, deja para siguiente tick

    count = drain("handoff", consumer)

    assert count == 0
    assert "wal:camael:handoff:inc-fail" in fake_redis.db


def test_drain_keeps_entry_on_consumer_exception(fake_redis):
    from storage.redis.wal import drain, enqueue

    enqueue("handoff", "inc-boom", {"id": 1}, ttl_seconds=3600)

    def consumer(payload: dict) -> bool:
        raise RuntimeError("camael unreachable")

    count = drain("handoff", consumer)

    assert count == 0
    assert "wal:camael:handoff:inc-boom" in fake_redis.db


def test_pending_count_returns_number_of_entries(fake_redis):
    from storage.redis.wal import enqueue, pending_count

    assert pending_count("handoff") == 0
    enqueue("handoff", "a", {}, ttl_seconds=60)
    enqueue("handoff", "b", {}, ttl_seconds=60)
    enqueue("rfc_update", "c", {}, ttl_seconds=60)
    assert pending_count("handoff") == 2
    assert pending_count("rfc_update") == 1
