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

    if not isinstance(parsed_json, dict):
        raise QwenError("Qwen JSON response must be an object")

    required_top_level = set(schema.get("required", []))
    actual_top_level = set(parsed_json.keys())
    if actual_top_level != required_top_level:
        raise QwenError(f"Qwen JSON keys must be exactly {sorted(required_top_level)}, got {sorted(actual_top_level)}")

    role_rules = schema.get("properties", {}).get("role", {})
    allowed_roles = role_rules.get("enum", [])
    role = parsed_json.get("role")
    if role not in allowed_roles:
        raise QwenError(f"Qwen role must be one of {allowed_roles}, got {role!r}")

    result = parsed_json.get("result")
    if not isinstance(result, dict):
        raise QwenError("Qwen result must be an object")

    result_rules = schema.get("properties", {}).get("result", {})
    required_result_keys = set(result_rules.get("required", []))
    actual_result_keys = set(result.keys())
    if actual_result_keys != required_result_keys:
        raise QwenError(f"Qwen result keys must be exactly {sorted(required_result_keys)}, got {sorted(actual_result_keys)}")

    command_rules = result_rules.get("properties", {}).get("command", {})
    allowed_commands = command_rules.get("enum", [])
    command = result.get("command")
    if command not in allowed_commands:
        raise QwenError(f"Qwen command must be one of {allowed_commands}, got {command!r}")


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
