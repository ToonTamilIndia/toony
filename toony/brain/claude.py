"""Claude backend, on the official Anthropic SDK."""

from __future__ import annotations

from typing import Any, Callable

from ..log import get
from .base import Brain, BrainError, InvalidRequest, BrainReply, Message, ToolCall, ToolSpec

log = get("brain.claude")

# Models that take adaptive thinking and output_config.effort, and that reject
# `temperature`. Anything else is sent as a plain request.
_MODERN = ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
           "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5", "claude-mythos-5")

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeBrain(Brain):
    name = "claude"

    def __init__(self, model: str, api_key: str, max_tokens: int = 16000,
                 effort: str = "low", thinking: str = "adaptive",
                 refusal_fallback: bool = True, timeout: float = 60.0):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise BrainError(
                "The Claude backend needs the anthropic package: pip install 'toony[claude]'"
            ) from exc
        self._anthropic = anthropic
        # A bare client also picks up an `ant auth login` profile, so an empty
        # api_key is not necessarily an error.
        kwargs: dict[str, Any] = {"timeout": timeout}
        if api_key:
            kwargs["api_key"] = api_key
        self.client = anthropic.Anthropic(**kwargs)
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.thinking = thinking
        self.refusal_fallback = refusal_fallback

    # ---- request assembly -------------------------------------------------
    def _is_modern(self) -> bool:
        return self.model in _MODERN

    def _params(self, system: str, messages: list[Message],
                tools: list[ToolSpec]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if tools:
            params["tools"] = [t.to_anthropic() for t in tools]
        if self._is_modern():
            if self.thinking == "disabled" and self.effort in ("low", "medium", "high"):
                params["thinking"] = {"type": "disabled"}
            else:
                params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": self.effort}
        return params

    def _endpoint(self, params: dict[str, Any]):
        """Refusal fallback lives on the beta namespace; everything else does not."""
        if self.refusal_fallback and self._is_modern():
            return self.client.beta.messages, {
                **params, "betas": [_FALLBACK_BETA], "fallbacks": "default"}
        return self.client.messages, params

    # ---- the two entry points --------------------------------------------
    def reply(self, system: str, messages: list[Message],
              tools: list[ToolSpec]) -> BrainReply:
        endpoint, params = self._endpoint(self._params(system, messages, tools))
        try:
            response = endpoint.create(**params)
        except self._anthropic.APIStatusError as exc:
            if getattr(exc, "status_code", 0) == 400:
                # The transcript itself was refused; retrying it unchanged will
                # fail identically, so the agent needs to know to drop it.
                raise InvalidRequest(_explain(exc)) from exc
            raise BrainError(_explain(exc)) from exc
        except self._anthropic.APIConnectionError as exc:
            raise BrainError("I could not reach the Claude API.") from exc
        return self._to_reply(response)

    def stream_reply(self, system: str, messages: list[Message],
                     tools: list[ToolSpec],
                     on_text: Callable[[str], None] | None = None) -> BrainReply:
        endpoint, params = self._endpoint(self._params(system, messages, tools))
        try:
            with endpoint.stream(**params) as stream:
                if on_text:
                    for chunk in stream.text_stream:
                        on_text(chunk)
                else:
                    for _ in stream.text_stream:
                        pass
                response = stream.get_final_message()
        except self._anthropic.APIStatusError as exc:
            if getattr(exc, "status_code", 0) == 400:
                # The transcript itself was refused; retrying it unchanged will
                # fail identically, so the agent needs to know to drop it.
                raise InvalidRequest(_explain(exc)) from exc
            raise BrainError(_explain(exc)) from exc
        except self._anthropic.APIConnectionError as exc:
            raise BrainError("I could not reach the Claude API.") from exc
        return self._to_reply(response)

    def _to_reply(self, response: Any) -> BrainReply:
        # stop_details is populated only on a refusal, so guard before reading it.
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            reason = getattr(details, "category", None) or "policy"
            raise BrainError(f"Claude declined that request ({reason}).")
        text_parts, calls = [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
        usage = getattr(response, "usage", None)
        return BrainReply(
            text="".join(text_parts).strip(),
            tool_calls=calls,
            stop_reason=getattr(response, "stop_reason", "") or "",
            usage={"input": getattr(usage, "input_tokens", 0),
                   "output": getattr(usage, "output_tokens", 0)} if usage else {},
        )

    def check(self) -> str:
        try:
            self.client.models.retrieve(self.model)
            return f"reachable, model {self.model} available"
        except Exception as exc:  # pragma: no cover - network dependent
            return f"unreachable: {exc.__class__.__name__}: {exc}"


def _explain(exc: Any) -> str:
    status = getattr(exc, "status_code", None)
    if status == 401:
        return "My Claude API key was rejected."
    if status == 404:
        return "That Claude model does not exist for this API key."
    if status == 429:
        return "The Claude API is rate limiting me. Try again in a moment."
    if status and status >= 500:
        return "The Claude API is having trouble right now."
    return f"The Claude API returned an error: {exc}"
