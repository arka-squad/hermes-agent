"""Host contract and fail-closed policy for governed external memory.

Pure policy: no filesystem, provider imports, network or agent side effects.
"""
from dataclasses import dataclass

HOST_CAPABILITIES = frozenset({
    "execution_context.v1", "native_memory_mediation.v1",
    "qualified_context.v1", "required_initialization.v1", "durable_tool_capture.v1",
    # The host hands each outgoing request (id + exact messages) to providers
    # that ask for it, so a served context can be attested as INCLUDED — never
    # as used. A host without this capability cannot claim inclusion.
    "context_inclusion.v1",
})


class MemoryProviderError(RuntimeError):
    """The selected memory policy cannot be honoured by this runtime."""


@dataclass(frozen=True)
class MemoryPolicy:
    provider: str = ""
    required: bool = False
    native_context: str = "builtin"

    @classmethod
    def from_config(cls, config: dict) -> "MemoryPolicy":
        mem = config.get("memory", {})
        if not isinstance(mem, dict):
            raise MemoryProviderError("memory must be a mapping")
        provider = mem.get("provider", "") or ""
        required = mem.get("provider_required", False)
        native = mem.get("native_context", "builtin")
        if not isinstance(provider, str) or type(required) is not bool:
            raise MemoryProviderError("Invalid memory provider selection")
        provider = provider.strip()
        if provider and (len(provider) > 64 or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in provider)):
            raise MemoryProviderError("Invalid memory provider name")
        if native not in {"builtin", "provider"}:
            raise MemoryProviderError("memory.native_context must be builtin or provider")
        if (required or native == "provider") and not provider:
            raise MemoryProviderError("A governed/required memory policy needs a selected provider")
        return cls(provider, required, native)

    @property
    def prompt_marker(self) -> str:
        if self.native_context == "provider":
            return f"Memory context policy: provider/{self.provider}/v1"
        return ""


def validate_restored_memory_policy(agent, stored_prompt: str) -> None:
    """Reject a pre-governance session without rewriting its cached history."""
    marker = getattr(agent, "_memory_context_policy", "")
    if isinstance(marker, str) and marker and marker not in stored_prompt.splitlines():
        raise MemoryProviderError(
            "This session predates the selected governed memory policy. "
            "Start a new session; the existing history and memory files are preserved."
        )
