"""Shared fixtures for Mattermost backend tests."""

import pytest

from tests.fakes import FakePlatform


@pytest.fixture
def fake_platform():
    """Return a FakePlatform instance for testing."""
    return FakePlatform()
