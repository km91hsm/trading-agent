import pytest
from fastapi.testclient import TestClient
from helpers_web import make_deps

from trader.web.app import create_app


@pytest.fixture
def deps(project, conn):
    return make_deps(project)


@pytest.fixture
def client(deps):
    return TestClient(create_app(deps))
