"""The Cortex Cognitive Provider routes mounted by `web_server`.

They read the ACTIVE profile only, never a caller path; the activation
operation is a private, single-use, expiring file; the status never says
"connected" from a config file alone.
"""
import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hermes_cli.web_server import _SESSION_TOKEN, app

client = TestClient(app)
HEADERS = {"X-Hermes-Session-Token": _SESSION_TOKEN}
PLUGIN_SOURCE = Path(__file__).resolve().parents[2].parent.parent / "LABS_PRODUCTS" / "cortex.deck_officiel" / "depot_source_osx" / "Hermers-plugin" / "cortex"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.cortex_routes.CORTEX_APP_PATHS", (str(tmp_path / "Cortex.app"),))
    import sys
    for name in [m for m in sys.modules if m.startswith("_hermes_cortex_plugin")]:
        sys.modules.pop(name)
    return tmp_path


def install_plugin(home: Path):
    if not PLUGIN_SOURCE.is_dir():
        pytest.skip("plugin source checkout not available")
    shutil.copytree(PLUGIN_SOURCE, home / "plugins" / "cortex")


def test_status_without_cortex_names_the_install_path_and_activation_is_refused(home):
    response = client.get("/api/cortex/status", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["cortex"] == "missing"
    assert body["plugin"]["installed"] is False
    assert body["action"] == "install_cortex"
    refused = client.post("/api/cortex/activation", headers=HEADERS, json={})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "target_missing"
    assert not list(home.glob("cortex/activation-*.json"))


def test_activation_creates_a_private_single_use_operation_readable_by_its_id(home):
    (home / "Cortex.app").mkdir()
    started = client.post("/api/cortex/activation", headers=HEADERS, json={})
    assert started.status_code == 200, started.text
    body = started.json()
    operation_id = body["operationId"]
    assert body["deepLink"] == "cortex://hermes/activate?op=" + operation_id
    path = home / "cortex" / ("activation-" + operation_id + ".json")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    record = json.loads(path.read_text())
    assert record["state"] == "pending" and "token" not in json.dumps(record)
    polled = client.get("/api/cortex/activation/" + operation_id, headers=HEADERS)
    assert polled.status_code == 200
    assert polled.json()["state"] == "pending"
    assert polled.json()["status"]["action"] == "activate"
    unknown = client.get("/api/cortex/activation/hop-ffffffffffffffffffffffffffffffff", headers=HEADERS)
    assert unknown.status_code == 404
    traversal = client.get("/api/cortex/activation/..%2Fetc", headers=HEADERS)
    assert traversal.status_code in (404, 422)


def test_status_with_plugin_and_binding_reports_facts_not_a_guessed_connection(home):
    (home / "Cortex.app").mkdir()
    install_plugin(home)
    from hashlib import sha256
    (home / "cortex").mkdir()
    binding = home / "cortex" / "binding.json"
    binding.write_text(json.dumps({
        "bindingId": "hb-1", "endpoint": "http://127.0.0.1:1", "token": "secret", "installation": "i",
        "profile": sha256(str(home.resolve()).encode()).hexdigest(), "issuedAt": "2026-09-13T22:00:00+00:00", "revision": 1,
    }))
    binding.chmod(0o600)
    (home / "config.yaml").write_text("memory:\n  provider: cortex\n  provider_required: true\n  native_context: provider\n")
    response = client.get("/api/cortex/status", headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plugin"] == {"installed": True, "version": "0.2.0"}
    assert body["binding"]["present"] is True and body["binding"]["bindingId"] == "hb-1"
    assert body["governed"] is True
    assert body["action"] == "none"
    # Cortex is not listening on port 1: the remote state is unknown, not guessed.
    assert body["remote"] is None
    assert body["outbox"] == {"queued": 0, "refused": 0}
    assert "secret" not in response.text


def test_routes_require_the_session_token(home):
    assert client.get("/api/cortex/status").status_code in (401, 403)
