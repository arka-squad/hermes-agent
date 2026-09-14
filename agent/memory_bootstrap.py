"""Compose native memory and an optional external provider for one agent.

Provider selection remains profile-scoped. A required provider fails closed;
legacy optional providers retain their existing best-effort behaviour.

Ported to Hermes 0.21.3: the built-in store follows ``agent_init`` (toolset
request, ``get_builtin_memory_*`` helpers) and the provider kwargs reuse
``_memory_provider_init_kwargs`` so identity scoping cannot drift.
"""
from __future__ import annotations

import logging

from agent.memory_runtime import HOST_CAPABILITIES, MemoryProviderError, MemoryPolicy

logger = logging.getLogger(__name__)


def validate_memory_config_files() -> None:
    """The native config loader may warn then return defaults on broken YAML.

    That fallback is unsafe when the unreadable file selected governed memory.
    Validate the two native sources before trusting the merged policy.
    """
    from hermes_cli.config import get_config_path
    from hermes_cli.managed_scope import get_managed_dir
    from utils import fast_safe_load

    paths = [get_config_path()]
    managed = get_managed_dir()
    if managed is not None:
        paths.append(managed / "config.yaml")
    for path in paths:
        try:
            with path.open(encoding="utf-8") as source:
                raw = fast_safe_load(source)
            if raw is not None and (not isinstance(raw, dict) or not isinstance(raw.get("memory", {}), dict)):
                raise ValueError("Invalid memory configuration structure")
        except FileNotFoundError:
            continue
        except Exception:
            raise MemoryProviderError("Memory policy cannot be verified: repair the Hermes configuration") from None


def _init_native_store(agent, config: dict, *, skip_memory: bool, policy: MemoryPolicy) -> None:
    """Built-in MEMORY.md / USER.md store, as ``agent_init`` loads it: skip_memory skips the
    external provider, but an explicitly requested ``memory`` toolset still gets the store so
    the memory tool never sees ``store=None`` (#65429). A denylisted toolset is not a request."""
    agent._memory_store = None
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent._memory_nudge_interval = 10
    agent._turns_since_memory = 0
    agent._iters_since_skill = 0
    toolset_requested = (
        "memory" in (getattr(agent, "enabled_toolsets", None) or [])
        and "memory" not in (getattr(agent, "disabled_toolsets", None) or [])
    )
    if skip_memory and not toolset_requested:
        return
    try:
        from tools.memory_tool import MemoryStore, get_builtin_memory_config, get_builtin_memory_store_flags

        mem_config = get_builtin_memory_config(config)
        agent._memory_enabled, agent._user_profile_enabled = get_builtin_memory_store_flags(config)
        agent._memory_nudge_interval = int(mem_config.get("nudge_interval", 10))
        if agent._memory_enabled or agent._user_profile_enabled:
            agent._memory_store = MemoryStore(
                memory_char_limit=mem_config.get("memory_char_limit", 2200),
                user_char_limit=mem_config.get("user_char_limit", 1375),
                memory_enabled=agent._memory_enabled,
                user_profile_enabled=agent._user_profile_enabled,
            )
            agent._memory_store.load_from_disk()
    except Exception:
        if policy.required:
            raise MemoryProviderError("Native memory could not be loaded safely") from None
        logger.warning("Optional native memory initialization failed", exc_info=True)


def _provider_kwargs(agent, platform: str) -> dict:
    """The scoping kwargs ``agent_init._memory_provider_init_kwargs`` builds, read
    tolerantly: a provider is initialized from whatever identity the agent carries,
    never refused because an optional attribute is absent."""
    from contextlib import suppress

    from agent.agent_init import _GATEWAY_IDENTITY_PARAMS
    from hermes_constants import get_hermes_home

    kwargs = {"session_id": agent.session_id, "platform": platform or "cli", "hermes_home": str(get_hermes_home())}
    if kwargs["platform"] == "cli":
        for key, attr in (("warning_callback", "_emit_warning"), ("status_callback", "_emit_status")):
            callback = getattr(agent, attr, None)
            if callback is not None:
                kwargs[key] = callback
    db = getattr(agent, "_session_db", None)
    if db:
        with suppress(Exception):
            title = db.get_session_title(agent.session_id)
            if title:
                kwargs["session_title"] = title
    for ident in _GATEWAY_IDENTITY_PARAMS:
        value = getattr(agent, f"_{ident}", None)
        if value:
            kwargs[ident] = value
    with suppress(Exception):
        from hermes_cli.profiles import get_active_profile_name
        kwargs["agent_identity"] = get_active_profile_name()
        kwargs["agent_workspace"] = "hermes"
    return kwargs


def initialize_memory(agent, config: dict, *, skip_memory: bool, platform: str) -> None:
    from agent.agent_init import _warn_memory_provider_unavailable
    from agent.memory_manager import MemoryManager
    from plugins.memory import load_memory_provider

    validate_memory_config_files()
    policy = MemoryPolicy.from_config(config)
    agent._native_memory_mediated = policy.native_context == "provider"
    agent._memory_context_policy = policy.prompt_marker
    agent._memory_provider_status = {"state": "disabled", "provider": policy.provider}
    agent._memory_manager = None
    _init_native_store(agent, config, skip_memory=skip_memory, policy=policy)

    if not policy.provider:
        return
    try:
        provider = load_memory_provider(policy.provider)
        if provider is None:
            raise MemoryProviderError("Selected provider is not installed or could not be loaded")
        if skip_memory and not getattr(provider, "capture_without_native_memory", False):
            if policy.native_context == "provider":
                raise MemoryProviderError("Provider cannot capture while personal memory is disabled")
            agent._memory_provider_status["state"] = "disabled_for_context"
            return
        if not provider.is_available():
            if not (policy.required or policy.native_context == "provider"):
                # Legacy optional providers keep the host's once-per-process warning.
                reason = ""
                try:
                    reason = provider.unavailable_reason()
                except Exception:
                    logger.debug("unavailable_reason() failed", exc_info=True)
                _warn_memory_provider_unavailable(policy.provider, reason)
            raise MemoryProviderError("Selected provider is not configured or is incompatible")
        required = getattr(provider, "required_runtime_capabilities", frozenset())
        missing = set(required) - HOST_CAPABILITIES
        if missing:
            raise MemoryProviderError("Missing host capabilities: " + ", ".join(sorted(missing)))
        context = platform if platform in {"cron", "subagent"} else "flush" if skip_memory else "primary"
        kwargs = _provider_kwargs(agent, platform)
        kwargs.update({
            "agent_context": context,
            "parent_session_id": getattr(agent, "_parent_session_id", None) or "",
            "host_capabilities": HOST_CAPABILITIES,
            "native_memory_mediated": agent._native_memory_mediated,
        })
        # Initialization is synchronous and must succeed before advertising tools.
        # The manager's initialize_all intentionally swallows failures.
        try:
            provider.initialize(**kwargs)
            manager = MemoryManager()
            manager.add_provider(provider)
        except Exception:
            try:
                provider.shutdown()
            except Exception:
                logger.debug("Provider cleanup after failed initialization failed", exc_info=True)
            raise
        agent._memory_manager = manager
        agent._memory_provider_status["state"] = "active"
        logger.info("Memory provider '%s' activated (%s)", policy.provider, context)
    except Exception:
        agent._memory_provider_status["state"] = "unavailable"
        # Provider errors may contain endpoint credentials or source text.
        # Expose a fixed diagnostic rather than passing arbitrary exception text.
        message = f"Memory provider '{policy.provider}' unavailable; verify installation and configuration"
        if policy.required or policy.native_context == "provider":
            raise MemoryProviderError(message) from None
        logger.warning(message)
