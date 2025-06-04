"""
Configuration des tests pytest.
"""

import pytest
from app import create_app
from app.config import TestingConfig


@pytest.fixture
def app():
    """Fixture pour créer une instance de l'application de test."""
    app = create_app(TestingConfig)
    
    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    """Fixture pour créer un client de test."""
    return app.test_client()


@pytest.fixture
def runner(app):
    """Fixture pour créer un runner CLI de test."""
    return app.test_cli_runner()