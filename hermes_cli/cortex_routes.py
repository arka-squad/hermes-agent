"""HTTP routes for the Cortex Cognitive Provider activation, mounted by
``web_server`` like the memory OAuth routes.

The Desktop settings view calls these; nothing here talks to a model or
reads memory. The plugin installed in the profile (``plugins/cortex``) does
the profile work; when it is absent, the routes say so and the view offers
the guided path. Every route is scoped to the active profile or to an
explicit profile name, never to a caller-supplied directory.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/cortex")

CORTEX_APP_PATHS = ("/Applications/Cortex.app",)
VERIFY_TIMEOUT_SECONDS = 90


@contextmanager
def _scope_to_profile(profile: Optional[str]):
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        yield
        return
    from hermes_cli import profiles as profiles_mod
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    try:
        profiles_mod.validate_profile_name(requested)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not profiles_mod.profile_exists(requested):
        raise HTTPException(status_code=404, detail=f"Profile '{requested}' does not exist.")
    token = set_hermes_home_override(str(profiles_mod.get_profile_dir(requested)))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _plugin_dir() -> Path:
    return _home() / "plugins" / "cortex"


def _load_plugin_module(name: str):
    """Import a module of the INSTALLED plugin (``$HERMES_HOME/plugins/cortex``)
    under an isolated package name, so the code that runs is the code Cortex
    installed, not a bundled copy."""
    directory = _plugin_dir()
    init = directory / "__init__.py"
    if not init.is_file():
        return None
    package = "_hermes_cortex_plugin"
    if package not in sys.modules:
        spec = importlib.util.spec_from_file_location(package, init, submodule_search_locations=[str(directory)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    full = f"{package}.{name}"
    if full not in sys.modules:
        spec = importlib.util.spec_from_file_location(full, directory / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
    return sys.modules[full]


def _cortex_installed() -> str:
    for path in CORTEX_APP_PATHS:
        if Path(path).is_dir():
            return "installed"
    return "missing"


def _profile_label() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name()
    except Exception:
        return "default"


def _status_payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cortex": _cortex_installed(),
        "plugin": {"installed": (_plugin_dir() / "__init__.py").is_file(), "version": None},
        "binding": None, "policy": None, "runtime": None, "action": "activate",
    }
    activation = _load_plugin_module("activation")
    if activation is None:
        if payload["cortex"] == "missing":
            payload["action"] = "install_cortex"
        return payload
    state = activation.activation_state(_home())
    payload["plugin"]["version"] = state.get("pluginVersion")
    payload["capabilities"] = state.get("capabilities", [])
    payload["binding"] = state.get("binding")
    payload["policy"] = state.get("policy")
    payload["governed"] = bool(state.get("governed"))
    payload["remote"] = None
    payload["outbox"] = None
    if state.get("binding", {}).get("present"):
        payload["remote"] = _remote_state(state["binding"]["bindingId"])
        payload["outbox"] = _outbox_state()
    if not state.get("binding", {}).get("present"):
        payload["action"] = "activate" if payload["cortex"] == "installed" else "install_cortex"
    elif not state.get("governed"):
        payload["action"] = "reactivate"
    else:
        payload["action"] = "none"
    return payload


def _remote_state(binding_id: str) -> Optional[dict[str, Any]]:
    """What Cortex says of the binding, when reachable: desired state and
    revision. `None` = Cortex unavailable — never a guessed state."""
    transport_module = _load_plugin_module("transport")
    if transport_module is None:
        return None
    try:
        answer = transport_module.LocalTransport(_home(), timeout=1.5).post("/v1/hermes/state", {"bindingId": binding_id})
    except Exception:
        return None
    return {"desiredState": answer.get("desiredState"), "revision": answer.get("revision"), "paused": bool(answer.get("paused"))}


def _outbox_state() -> Optional[dict[str, Any]]:
    journal_module = _load_plugin_module("journal")
    if journal_module is None:
        return None
    try:
        journal = journal_module.Journal(_home())
    except Exception:
        return None
    try:
        counts = journal.status()
    finally:
        journal.close()
    return {"queued": int(counts.get("queued", 0)), "refused": int(counts.get("refused", 0)) + int(counts.get("rejected", 0))}


@router.get("/status")
async def cortex_status(profile: Optional[str] = None):
    with _scope_to_profile(profile):
        return _status_payload()


class ActivationRequest(BaseModel):
    profile: Optional[str] = None


@router.post("/activation")
async def cortex_start_activation(body: ActivationRequest):
    """Create a single-use, expiring operation in the profile and return the
    deep link Cortex completes. No secret travels; Cortex reads the operation
    from the profile directory it discovered."""
    with _scope_to_profile(body.profile):
        if _cortex_installed() != "installed":
            raise HTTPException(status_code=409, detail={"code": "target_missing", "message": "Cortex n'est pas installé"})
        activation = _load_plugin_module("activation")
        if activation is None:
            # The plugin is installed by Cortex during the guided activation;
            # the operation file is enough for Cortex to find this profile.
            record = _create_operation_without_plugin(_home(), _profile_label())
        else:
            try:
                record = activation.create_operation(_home(), _profile_label())
            except Exception as error:
                code = getattr(error, "code", "durability_failed")
                raise HTTPException(status_code=500, detail={"code": code, "message": str(error)})
        return {"operationId": record["operationId"], "deepLink": record["deepLink"], "expiresAt": record["expiresAt"]}


@router.get("/activation/{operation_id}")
async def cortex_activation_state(operation_id: str, profile: Optional[str] = None):
    with _scope_to_profile(profile):
        activation = _load_plugin_module("activation")
        if activation is None:
            record = _read_operation_without_plugin(_home(), operation_id)
        else:
            try:
                record = activation.read_operation(_home(), operation_id)
            except Exception as error:
                code = getattr(error, "code", "action_unknown")
                raise HTTPException(status_code=404, detail={"code": code, "message": str(error)})
        return {"state": record.get("state", "pending"), "operationId": operation_id, "status": _status_payload()}


@router.post("/verify")
async def cortex_verify(body: ActivationRequest):
    """Run the plugin's diagnostic runtime for this profile with THIS
    interpreter. It prepares the verification (spec §5.2) and returns the
    runtime's own attestation state; it does not touch a running session."""
    with _scope_to_profile(body.profile):
        script = _plugin_dir() / "verify.py"
        if not script.is_file():
            raise HTTPException(status_code=409, detail={"code": "target_missing", "message": "plugin Cortex absent"})
        env = dict(os.environ, HERMES_HOME=str(_home()))
        env.pop("TERMINAL_CWD", None)
        try:
            completed = subprocess.run(
                [sys.executable, str(script), "--profile", str(_home())],
                capture_output=True, text=True, timeout=VERIFY_TIMEOUT_SECONDS, env=env, check=False,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail={"code": "runtime_unavailable", "message": "vérification trop longue"})
        line = next((l for l in completed.stdout.splitlines() if l.startswith("CORTEX_RUNTIME=")), None)
        if line is None:
            raise HTTPException(status_code=502, detail={"code": "runtime_unavailable", "message": "aucune attestation du runtime"})
        result = json.loads(line[len("CORTEX_RUNTIME="):])
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail={"code": result.get("code", "runtime_unavailable"), "message": result.get("message", "")})
        return result


def _create_operation_without_plugin(home: Path, profile_label: str) -> dict:
    """Same record as ``activation.create_operation``: before the plugin is
    installed, the Hermes backend can still open the guided path."""
    from datetime import datetime, timedelta, timezone
    from hashlib import sha256
    from uuid import uuid4

    directory = home / "cortex"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise HTTPException(status_code=500, detail={"code": "durability_failed", "message": "dossier Cortex non sûr"})
    operation_id = "hop-" + uuid4().hex
    now = datetime.now(timezone.utc)
    record = {
        "operationId": operation_id, "profile": sha256(str(home.resolve()).encode()).hexdigest(),
        "profileLabel": profile_label, "home": str(home.resolve()), "createdAt": now.isoformat(),
        "expiresAt": (now + timedelta(minutes=15)).isoformat(), "state": "pending",
    }
    path = directory / ("activation-" + operation_id + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    record["deepLink"] = "cortex://hermes/activate?op=" + operation_id
    return record


def _read_operation_without_plugin(home: Path, operation_id: str) -> dict:
    if not operation_id.startswith("hop-") or len(operation_id) != 36:
        raise HTTPException(status_code=404, detail={"code": "action_unknown", "message": "opération invalide"})
    path = home / "cortex" / ("activation-" + operation_id + ".json")
    if not path.is_file() or path.is_symlink():
        raise HTTPException(status_code=404, detail={"code": "action_unknown", "message": "opération inconnue"})
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _shutil_available() -> bool:  # pragma: no cover - keeps the import honest for packagers
    return shutil.which("python3") is not None
