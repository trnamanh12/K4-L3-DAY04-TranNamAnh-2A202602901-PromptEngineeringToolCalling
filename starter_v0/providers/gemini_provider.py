from collections import deque
import json
import os
import re
import threading
import time
from typing import Any

from providers.base import ModelResponse, ToolCall


class RateLimiter:
    """Sliding window and interval rate limiter to cap requests per minute."""

    def __init__(self, max_requests: int = 15, period: float = 60.0) -> None:
        self.max_requests = max_requests
        self.period = period
        self.min_interval = period / max_requests  # e.g., 4.0s for 15 RPM
        self.timestamps: deque[float] = deque()
        self._last_call: float = 0.0
        self._lock = threading.Lock()

    def wait_if_needed(self) -> None:
        with self._lock:
            now = time.time()
            # 1. Slide window: discard timestamps older than period (60s)
            while self.timestamps and now - self.timestamps[0] >= self.period:
                self.timestamps.popleft()

            # If quota for the window is reached, sleep until earliest timestamp expires
            if len(self.timestamps) >= self.max_requests:
                sleep_duration = (self.timestamps[0] + self.period) - now
                if sleep_duration > 0:
                    time.sleep(sleep_duration)
                now = time.time()
                while self.timestamps and now - self.timestamps[0] >= self.period:
                    self.timestamps.popleft()

            # 2. Smooth bursts: enforce minimum interval between consecutive calls
            elapsed = now - self._last_call
            if elapsed < self.min_interval:
                sleep_duration = self.min_interval - elapsed
                time.sleep(sleep_duration)
                now = time.time()

            self.timestamps.append(now)
            self._last_call = now


def _to_gemini_declarations(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for item in tools or []:
        function = item.get("function", item)
        declarations.append({
            "name": function["name"],
            "description": function.get("description", ""),
            "parameters": function.get("parameters", {"type": "object", "properties": {}}),
        })
    return declarations


def _to_gemini_contents(messages: list[dict[str, str]]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            system_parts.append(content)
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": content}]})
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": content}]})
    return ("\n\n".join(system_parts) if system_parts else None), contents


def _part_text(part: Any) -> str | None:
    if hasattr(part, "text"):
        return getattr(part, "text")
    if isinstance(part, dict):
        return part.get("text")
    return None


def _part_function_call(part: Any) -> Any | None:
    if hasattr(part, "function_call"):
        return getattr(part, "function_call")
    if isinstance(part, dict):
        return part.get("function_call")
    return None


def _function_call_name(call: Any) -> str | None:
    if hasattr(call, "name"):
        return getattr(call, "name")
    if isinstance(call, dict):
        return call.get("name")
    return None


def _function_call_args(call: Any) -> dict[str, Any]:
    if hasattr(call, "args"):
        return dict(getattr(call, "args") or {})
    if isinstance(call, dict):
        return dict(call.get("args") or {})
    return {}


class GeminiProvider:
    """Google Gemini API provider with normalized tool_calls output."""

    def __init__(
        self,
        *,
        api_key_env: str = "GEMINI_API_KEY",
        default_model: str = "gemini-3.5-flash-lite",
        requests_per_minute: int = 15,
        max_retries: int = 5,
        base_delay: float = 4.0,
    ) -> None:
        self.api_key_env = api_key_env
        self.default_model = default_model
        self.rate_limiter = RateLimiter(max_requests=requests_per_minute, period=60.0)
        self.max_retries = max_retries
        self.base_delay = base_delay

    def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        tool_choice: Any | None = None,
    ) -> ModelResponse:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("Install live provider dependency first: pip install google-genai") from exc

        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise RuntimeError(f"Missing API key env var: {self.api_key_env}")

        system_instruction, contents = _to_gemini_contents(messages)
        declarations = _to_gemini_declarations(tools)
        config_kwargs: dict[str, Any] = {"temperature": temperature}
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if declarations:
            config_kwargs["tools"] = [types.Tool(function_declarations=declarations)]
            if tool_choice == "required":
                try:
                    config_kwargs["tool_config"] = types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.ANY
                        )
                    )
                except Exception:
                    pass
            elif tool_choice == "none":
                try:
                    config_kwargs["tool_config"] = types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode=types.FunctionCallingConfigMode.NONE
                        )
                    )
                except Exception:
                    pass

        client = genai.Client(api_key=api_key)

        resp = None
        for attempt in range(self.max_retries):
            # Enforce 15 RPM limit before initiating the request
            self.rate_limiter.wait_if_needed()
            try:
                resp = client.models.generate_content(
                    model=model or self.default_model,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                break
            except Exception as exc:
                exc_str = str(exc)
                is_rate_limit = any(
                    term in exc_str.lower()
                    for term in ["429", "resource_exhausted", "quota", "rate limit"]
                )
                is_transient = any(
                    term in exc_str.lower()
                    for term in ["503", "unavailable", "timeout", "server error"]
                )

                if (is_rate_limit or is_transient) and attempt < self.max_retries - 1:
                    sleep_time = self.base_delay * (2 ** attempt)
                    match = re.search(r"retry\s+(?:in|after)\s+(\d+(?:\.\d+)?)s?", exc_str, re.IGNORECASE)
                    if not match:
                        match = re.search(r"seconds:\s*(\d+)", exc_str, re.IGNORECASE)
                    if match:
                        sleep_time = max(sleep_time, float(match.group(1)) + 1.0)

                    print(
                        f"[GeminiProvider] Rate limit / transient error on attempt {attempt + 1}/{self.max_retries}. "
                        f"Retrying in {sleep_time:.1f}s... (Detail: {exc_str[:120]})",
                        flush=True,
                    )
                    time.sleep(sleep_time)
                    continue
                raise

        text_parts: list[str] = []
        calls: list[ToolCall] = []

        def append_call(function_call: Any) -> None:
            name = _function_call_name(function_call)
            if name:
                calls.append(ToolCall(name=name, args=_function_call_args(function_call)))

        for candidate in getattr(resp, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", []) or []:
                text = _part_text(part)
                if text:
                    text_parts.append(text)
                function_call = _part_function_call(part)
                if function_call:
                    append_call(function_call)

        # Some SDK versions expose function calls directly on the response.
        for function_call in getattr(resp, "function_calls", []) or []:
            append_call(function_call)

        deduped_calls: list[ToolCall] = []
        seen: set[tuple[str, str]] = set()
        for call in calls:
            key = (call.name, json.dumps(call.args, ensure_ascii=False, sort_keys=True))
            if key not in seen:
                seen.add(key)
                deduped_calls.append(call)

        return ModelResponse(text="\n".join(part for part in text_parts if part) or None, tool_calls=deduped_calls, raw=resp)
