import json
import urllib.error
import urllib.request
import socket
from typing import Any, Dict, List, Optional


class LLMClient:
    def __init__(self, provider: str = "openai", api_key: str = "", model: str = "gpt-4.1-mini", temperature: float = 0.2) -> None:
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.temperature = temperature

    def _call_openai_chat(self, messages: List[Dict[str, str]],
                          tools: Optional[List[Dict]] = None) -> Dict[str, Any]:
        if not self.api_key:
            return {
                "ok": False,
                "status": "not_configured",
                "message": "OpenAI API key is missing.",
            }

        payload: Dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        result = self._post_openai_chat(payload)

        # Some models only accept the default temperature and reject explicit values.
        if (
            not result.get("ok", False)
            and result.get("status") == "http_error"
            and result.get("error_param") == "temperature"
            and result.get("error_code") == "unsupported_value"
            and "temperature" in payload
        ):
            fallback_payload = dict(payload)
            fallback_payload.pop("temperature", None)
            retry = self._post_openai_chat(fallback_payload)
            if retry.get("ok", False):
                retry["temperature_fallback_used"] = True
            return retry

        return result

    def _stream_openai_chat(
        self,
        payload: Dict[str, Any],
        on_chunk=None,
        on_thinking=None,
    ) -> Dict[str, Any]:
        """Stream chat completions via SSE. Collects and returns full result."""
        stream_payload = dict(payload)
        stream_payload["stream"] = True
        stream_payload["stream_options"] = {"include_usage": True}

        body = json.dumps(stream_payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        full_content: list = []
        usage: Dict[str, Any] = {}
        # Accumulate tool_calls across SSE deltas (function calling)
        tc_map: Dict[int, Dict[str, Any]] = {}  # index → partial tool call

        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        event = json.loads(data_str)
                    except Exception:
                        continue
                    if event.get("usage"):
                        usage = event["usage"]
                    choices = event.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    # Thinking / reasoning tokens (o1, o3, DeepSeek-R1 …)
                    thinking = (
                        delta.get("reasoning_content") or delta.get("thinking") or ""
                    )
                    if thinking and on_thinking:
                        on_thinking(thinking)
                    # Function-calling tool_calls deltas
                    for tc_delta in (delta.get("tool_calls") or []):
                        idx = tc_delta.get("index", 0)
                        if idx not in tc_map:
                            tc_map[idx] = {"id": "", "type": "function",
                                           "function": {"name": "", "arguments": ""}}
                        if tc_delta.get("id"):
                            tc_map[idx]["id"] = tc_delta["id"]
                        func = tc_delta.get("function") or {}
                        if func.get("name"):
                            tc_map[idx]["function"]["name"] += func["name"]
                        if func.get("arguments"):
                            tc_map[idx]["function"]["arguments"] += func["arguments"]
                    # Response content
                    chunk = delta.get("content") or ""
                    if chunk:
                        full_content.append(chunk)
                        if on_chunk:
                            on_chunk(chunk)

            if tc_map:
                tool_calls = [tc_map[i] for i in sorted(tc_map.keys())]
                return {
                    "ok": True, "status": "ok",
                    "content": "".join(full_content),
                    "tool_calls": tool_calls,
                    "usage": usage,
                }
            return {
                "ok": True,
                "status": "ok",
                "content": "".join(full_content),
                "usage": usage,
            }
        except (socket.timeout, TimeoutError) as error:
            return {
                "ok": False,
                "status": "timeout",
                "message": "OpenAI request timed out while reading the response.",
                "details": str(error),
            }
        except urllib.error.HTTPError as error:
            details = ""
            error_param = ""
            error_code = ""
            try:
                details = error.read().decode("utf-8")
                parsed = json.loads(details)
                api_error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
                if isinstance(api_error, dict):
                    error_param = str(api_error.get("param", ""))
                    error_code = str(api_error.get("code", ""))
            except Exception:
                details = str(error)
            return {
                "ok": False,
                "status": "http_error",
                "message": f"OpenAI HTTP error {error.code}",
                "details": details,
                "error_param": error_param,
                "error_code": error_code,
            }
        except Exception as error:
            return {"ok": False, "status": "request_error", "message": str(error)}

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        on_chunk=None,
        on_thinking=None,
        tools: "Optional[List[Dict]]" = None,
    ) -> Dict[str, Any]:
        """Streaming version of chat(). Falls back to non-streaming for other providers."""
        if self.provider != "openai":
            return self.chat(messages, tools=tools)
        payload: Dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        result = self._stream_openai_chat(payload, on_chunk=on_chunk, on_thinking=on_thinking)
        # Temperature fallback (same logic as chat())
        if (
            not result.get("ok", False)
            and result.get("status") == "http_error"
            and result.get("error_param") == "temperature"
            and result.get("error_code") == "unsupported_value"
        ):
            fallback = dict(payload)
            fallback.pop("temperature", None)
            return self._stream_openai_chat(fallback, on_chunk=on_chunk, on_thinking=on_thinking)
        return result

    def _post_openai_chat(self, payload: Dict[str, Any]) -> Dict[str, Any]:

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8")
            data = json.loads(raw)
            choices = data.get("choices", [])
            if not choices:
                return {"ok": False, "status": "bad_response", "message": "No choices in response", "raw": data}
            message = choices[0].get("message", {})
            content = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []
            usage = data.get("usage", {})
            result: Dict[str, Any] = {
                "ok": True,
                "status": "ok",
                "content": content,
                "usage": usage,
                "raw": data,
            }
            if tool_calls:
                result["tool_calls"] = tool_calls
            return result
        except (socket.timeout, TimeoutError) as error:
            return {
                "ok": False,
                "status": "timeout",
                "message": "OpenAI request timed out while reading the response.",
                "details": str(error),
            }
        except urllib.error.HTTPError as error:
            details = ""
            error_param = ""
            error_code = ""
            try:
                details = error.read().decode("utf-8")
                parsed = json.loads(details)
                api_error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
                if isinstance(api_error, dict):
                    error_param = str(api_error.get("param", ""))
                    error_code = str(api_error.get("code", ""))
            except Exception:
                details = str(error)
            return {
                "ok": False,
                "status": "http_error",
                "message": f"OpenAI HTTP error {error.code}",
                "details": details,
                "error_param": error_param,
                "error_code": error_code,
            }
        except Exception as error:
            return {
                "ok": False,
                "status": "request_error",
                "message": str(error),
            }

    def chat(self, messages: List[Dict[str, str]],
             tools: Optional[List[Dict]] = None) -> Dict[str, Any]:
        if self.provider != "openai":
            return {
                "ok": False,
                "status": "unsupported_provider",
                "message": f"Provider '{self.provider}' is not supported.",
            }
        return self._call_openai_chat(messages, tools=tools)

    def review(self, prompt: str, context: Dict[str, Any]) -> Dict[str, Any]:
        messages = [
            {"role": "system", "content": "You are an expert electronic design reviewer."},
            {"role": "user", "content": prompt},
        ]
        result = self.chat(messages)
        result["provider"] = self.provider
        return result
