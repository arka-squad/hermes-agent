"""Runtime adapter for providers requiring durable tool observations."""
import json
from agent.memory_runtime import MemoryProviderError

CAPTURE_CAPABILITY = "durable_tool_capture.v1"
INCLUSION_CAPABILITY = "context_inclusion.v1"


def capture_providers(agent):
    manager = getattr(agent, "_memory_manager", None)
    if manager is None:
        return []
    return [p for p in manager.providers
            if CAPTURE_CAPABILITY in getattr(p, "required_runtime_capabilities", ())]


def capture_event(agent, event):
    for provider in capture_providers(agent):
        provider.capture_event(event)


def capture_turn_start(agent, message):
    if not capture_providers(agent):
        return
    if getattr(agent, "_memory_capture_blocked", None):
        raise MemoryProviderError("Durable capture is suspended; reconcile the pending result before resuming")
    capture_event(agent, {
        "kind": "user_message", "content": message,
        "event_id": agent._current_turn_id + ":user",
        "turn_id": agent._current_turn_id,
        "metadata": {"turn_number": agent._user_turn_count},
    })


def execute_with_capture(agent, tool_name, args, tool_call_id, execute):
    if not capture_providers(agent):
        return execute(args)
    if getattr(agent, "_memory_capture_blocked", None):
        raise MemoryProviderError("Durable capture is suspended; no new tool can start")
    if not tool_call_id:
        raise MemoryProviderError("Durable capture requires the native tool call identity")
    metadata = {
        "tool_name": tool_name, "tool_call_id": tool_call_id,
        "api_request_id": getattr(agent, "_current_api_request_id", "") or "",
    }
    base = {"turn_id": getattr(agent, "_current_turn_id", "") or "", "metadata": metadata}
    # The intent is committed before any downstream side effect. An existing
    # intent is not permission to run again: the provider must reject it.
    try:
        capture_event(agent, dict(base, kind="tool_call", event_id=tool_call_id + ":intent",
                                  content=json.dumps(args, ensure_ascii=False)))
    except Exception:
        agent._memory_capture_blocked = "intent_not_durable"
        raise MemoryProviderError("Tool suspended: capture could not durably record its intent") from None
    try:
        result = execute(args)
    except BaseException:
        # An exception/cancellation does not establish absence of side effects.
        try:
            capture_event(agent, dict(base, kind="error", event_id=tool_call_id + ":result",
                                      content="Tool interrupted or raised; execution outcome unknown"))
        except Exception:
            agent._memory_capture_blocked = "result_not_durable"
        raise
    try:
        capture_event(agent, dict(base, kind="tool_result", event_id=tool_call_id + ":result",
                                  content=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)))
    except Exception:
        # Preserve the actual tool result for Hermes' transcript. Do not replace
        # it with a fabricated failure or retry an already executed action.
        agent._memory_capture_blocked = "result_not_durable"
    return result


def inclusion_providers(agent):
    manager = getattr(agent, "_memory_manager", None)
    if manager is None:
        return []
    return [p for p in manager.providers
            if INCLUSION_CAPABILITY in getattr(p, "required_runtime_capabilities", ())]


def capture_request(agent, request_id, messages):
    """The send boundary: the exact messages handed to the model adapter for
    this request id. Providers attest what they find there; the request is
    never blocked by an attestation failure — the provider journals it."""
    for provider in inclusion_providers(agent):
        hook = getattr(provider, "on_request_sent", None)
        if hook is None:
            continue
        try:
            hook(request_id, messages)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "memory provider %s could not attest request %s", getattr(provider, "name", "?"), request_id,
                exc_info=True,
            )


def capture_turn_end(agent, response, interrupted):
    if not capture_providers(agent) or not response:
        return
    turn_id = getattr(agent, "_current_turn_id", "")
    if not turn_id:
        return
    try:
        capture_event(agent, {
            "kind": "error" if interrupted else "assistant_message",
            "event_id": turn_id + ":assistant", "turn_id": turn_id,
            "content": response if isinstance(response, str) else json.dumps(response, ensure_ascii=False),
            "metadata": {"terminal": not interrupted, "interrupted": interrupted},
        })
    except Exception:
        agent._memory_capture_blocked = "turn_result_not_durable"
