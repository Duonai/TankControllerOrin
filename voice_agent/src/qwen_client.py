from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request


class QwenError(RuntimeError):
    pass


@dataclass(slots=True)
class QwenConfig:
    base_url: str = "http://127.0.0.1:8080"
    model_name: str = "qwen-local"
    timeout_seconds: int = 120
    temperature: float = 0.1
    max_tokens: int = 256


def load_prompt(prompt_path: Path) -> str:
    return prompt_path.read_text(encoding="utf-8").strip()


def load_schema(schema_path: Path) -> dict[str, Any]:
    return json.loads(schema_path.read_text(encoding="utf-8"))


class QwenClient:
    def __init__(self, config: QwenConfig) -> None:
        self.config = config

    def normalize_command(self, transcript: str, system_prompt: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "model": self.config.model_name,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript},
            ],
        }

        url = f"{self.config.base_url.rstrip('/')}/v1/chat/completions"
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")

        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise QwenError(f"Qwen HTTP error {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise QwenError(f"Qwen connection failed: {exc}") from exc

        data = json.loads(response_body)
        content = data["choices"][0]["message"]["content"].strip()

        parsed_json = None
        try:
            parsed_json = json.loads(_extract_json_block(content))
        except (json.JSONDecodeError, ValueError):
            parsed_json = None

        if schema is not None:
            validate_command_schema(parsed_json, schema)

        return {
            "transcript": transcript,
            "raw_response": data,
            "content": content,
            "parsed_json": parsed_json,
        }


def validate_command_schema(parsed_json: dict[str, Any] | None, schema: dict[str, Any]) -> None:
    if parsed_json is None:
        raise QwenError("Qwen response did not contain valid JSON")

    _validate_schema_value(parsed_json, schema, path="$")


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> None:
    expected_type = schema.get("type")

    if expected_type == "object":
        if not isinstance(value, dict):
            raise QwenError(f"Qwen JSON at {path} must be an object")

        properties = schema.get("properties", {})
        required_keys = set(schema.get("required", []))
        actual_keys = set(value.keys())

        missing_keys = sorted(required_keys - actual_keys)
        if missing_keys:
            raise QwenError(f"Qwen JSON at {path} is missing required keys: {missing_keys}")

        if schema.get("additionalProperties", True) is False:
            unexpected_keys = sorted(actual_keys - set(properties.keys()))
            if unexpected_keys:
                raise QwenError(f"Qwen JSON at {path} has unexpected keys: {unexpected_keys}")

        for key, child_schema in properties.items():
            if key in value:
                _validate_schema_value(value[key], child_schema, path=f"{path}.{key}")
        return

    if expected_type == "string":
        if not isinstance(value, str):
            raise QwenError(f"Qwen JSON at {path} must be a string")
    elif expected_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QwenError(f"Qwen JSON at {path} must be a number")
    elif expected_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise QwenError(f"Qwen JSON at {path} must be an integer")
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            raise QwenError(f"Qwen JSON at {path} must be a boolean")

    allowed_values = schema.get("enum")
    if allowed_values is not None and value not in allowed_values:
        raise QwenError(f"Qwen JSON at {path} must be one of {allowed_values}, got {value!r}")


def _extract_json_block(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("JSON object not found in Qwen response")
    return stripped[start : end + 1]
