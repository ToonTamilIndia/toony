"""Backend for any OpenAI-compatible chat completions endpoint.

Used for both the ``openai`` provider (api.openai.com, Groq, OpenRouter, vLLM,
llama.cpp, LM Studio, ...) and the ``ollama`` provider, which exposes the same
surface at http://localhost:11434/v1.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..log import get
from .base import (Brain, BrainError, BrainReply, InvalidRequest,
                   Message, ToolCall, ToolSpec)

log = get("brain.openai")


class OpenAICompatBrain(Brain):
    def __init__(self, model: str, base_url: str, api_key: str,
                 max_tokens: int = 2048, temperature: float = 0.5,
                 name: str = "openai", timeout: float = 60.0):
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise BrainError(
                "This backend needs the openai package: pip install 'toony[openai]'"
            ) from exc
        self._openai = openai
        self.client = openai.OpenAI(base_url=base_url or None,
                                    api_key=api_key or "not-needed",
                                    timeout=timeout)
        self.model = model
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.name = name
        # api.openai.com renamed the field on newer models; discovered on first 400.
        self._token_param = "max_tokens"

    # ---- transcript translation -------------------------------------------
    @staticmethod
    def _convert(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            tool_results = [b for b in msg.content if b.get("type") == "tool_result"]
            if tool_results:
                for block in tool_results:
                    out.append({"role": "tool",
                                "tool_call_id": block["tool_use_id"],
                                "content": str(block.get("content") or
                                               "(the tool returned nothing)")})
                continue

            if msg.role == "assistant":
                text = "".join(b.get("text", "") for b in msg.content
                               if b.get("type") == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"],
                                       "arguments": json.dumps(b.get("input") or {})}}
                         for b in msg.content if b.get("type") == "tool_use"]
                # An empty string, never null. OpenAI allows null content
                # beside tool_calls; Ollama's compatibility layer rejects it
                # with "invalid message content type: <nil>", and because the
                # conversation is stored, that then breaks every later turn.
                entry: dict[str, Any] = {"role": "assistant", "content": text}
                if calls:
                    entry["tool_calls"] = calls
                out.append(entry)
                continue

            parts: list[dict[str, Any]] = []
            for block in msg.content:
                if block.get("type") == "text":
                    parts.append({"type": "text", "text": block["text"]})
                elif block.get("type") == "image":
                    src = block["source"]
                    url = f"data:{src['media_type']};base64,{src['data']}"
                    parts.append({"type": "image_url", "image_url": {"url": url}})
            if len(parts) == 1 and parts[0]["type"] == "text":
                out.append({"role": "user", "content": parts[0]["text"]})
            else:
                out.append({"role": "user", "content": parts})
        return out

    def _params(self, system: str, messages: list[Message],
                tools: list[ToolSpec]) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + self._convert(messages),
            "temperature": self.temperature,
            self._token_param: self.max_tokens,
        }
        if tools:
            params["tools"] = [t.to_openai() for t in tools]
            params["tool_choice"] = "auto"
        return params

    def _create(self, params: dict[str, Any], stream: bool = False):
        try:
            return self.client.chat.completions.create(stream=stream, **params)
        except self._openai.BadRequestError as exc:
            message = str(exc)
            if "max_tokens" in message and "max_completion_tokens" in message:
                self._token_param = "max_completion_tokens"
                params["max_completion_tokens"] = params.pop("max_tokens")
                return self.client.chat.completions.create(stream=stream, **params)
            raise InvalidRequest(
                f"The model rejected the request: {exc}") from exc
        except self._openai.AuthenticationError as exc:
            raise BrainError("My API key for that model was rejected.") from exc
        except self._openai.APIConnectionError as exc:
            hint = (" Is Ollama running? Try: systemctl --user status ollama"
                    if "11434" in self.base_url else "")
            raise BrainError(f"I could not reach the model at {self.base_url}.{hint}") from exc
        except self._openai.APIStatusError as exc:
            raise BrainError(f"The model endpoint returned an error: {exc}") from exc

    # ---- entry points -----------------------------------------------------
    def reply(self, system: str, messages: list[Message],
              tools: list[ToolSpec]) -> BrainReply:
        response = self._create(self._params(system, messages, tools))
        choice = response.choices[0]
        calls = [_call(tc) for tc in (choice.message.tool_calls or [])]
        usage = getattr(response, "usage", None)
        return BrainReply(
            text=(choice.message.content or "").strip(),
            tool_calls=calls,
            stop_reason=choice.finish_reason or "",
            usage={"input": getattr(usage, "prompt_tokens", 0),
                   "output": getattr(usage, "completion_tokens", 0)} if usage else {},
        )

    def stream_reply(self, system: str, messages: list[Message],
                     tools: list[ToolSpec],
                     on_text: Callable[[str], None] | None = None) -> BrainReply:
        stream = self._create(self._params(system, messages, tools), stream=True)
        text_parts: list[str] = []
        # Tool call fragments arrive spread across chunks, keyed by index.
        partial: dict[int, dict[str, str]] = {}
        finish = ""
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            finish = choice.finish_reason or finish
            delta = choice.delta
            if getattr(delta, "content", None):
                text_parts.append(delta.content)
                if on_text:
                    on_text(delta.content)
            for frag in getattr(delta, "tool_calls", None) or []:
                slot = partial.setdefault(frag.index, {"id": "", "name": "", "args": ""})
                if frag.id:
                    slot["id"] = frag.id
                if frag.function and frag.function.name:
                    slot["name"] += frag.function.name
                if frag.function and frag.function.arguments:
                    slot["args"] += frag.function.arguments

        calls = []
        for index in sorted(partial):
            slot = partial[index]
            calls.append(ToolCall(slot["id"] or f"call_{index}", slot["name"],
                                  _loads(slot["args"])))
        return BrainReply(text="".join(text_parts).strip(), tool_calls=calls,
                          stop_reason=finish)

    def check(self) -> str:
        try:
            names = [m.id for m in self.client.models.list().data]
        except Exception as exc:  # pragma: no cover - network dependent
            return f"unreachable at {self.base_url}: {exc.__class__.__name__}"
        if self.model in names:
            return f"reachable, model {self.model} available"
        listed = ", ".join(names[:6]) or "none"
        return f"reachable, but model {self.model} is NOT installed (available: {listed})"


def _call(raw: Any) -> ToolCall:
    return ToolCall(raw.id, raw.function.name, _loads(raw.function.arguments))


def _loads(text: str) -> dict[str, Any]:
    """Tool arguments are model-generated JSON — never trust it to parse."""
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        log.warning("could not parse tool arguments: %r", text[:200])
        return {}
    return value if isinstance(value, dict) else {"value": value}
