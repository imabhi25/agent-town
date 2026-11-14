import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults_are_valid():
    s = Settings()
    assert s.heartbeat_ttl < s.lease_seconds


def test_heartbeat_ttl_must_be_shorter_than_lease():
    with pytest.raises(ValidationError, match="heartbeat_ttl"):
        Settings(heartbeat_ttl=20, lease_seconds=15)


def test_heartbeat_ttl_equal_to_lease_is_rejected():
    with pytest.raises(ValidationError, match="heartbeat_ttl"):
        Settings(heartbeat_ttl=15, lease_seconds=15)


def test_valid_custom_values_are_accepted():
    s = Settings(heartbeat_ttl=1, lease_seconds=3)
    assert (s.heartbeat_ttl, s.lease_seconds) == (1, 3)


def test_provider_timeout_must_be_positive():
    with pytest.raises(ValidationError):
        Settings(provider_timeout=0)
    assert Settings().provider_timeout == 30.0
