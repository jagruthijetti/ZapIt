import asyncio
import json
import re
from typing import Any, List, Tuple, Type, TypeVar, get_args, get_origin

import httpx
from google import genai
from google.genai import types
from pydantic import BaseModel, ValidationError

from backend.config import settings
from backend.schemas import ProviderType

T = TypeVar("T", bound=BaseModel)


class LLMRouterError(Exception):
    """Raised when Ollama and Gemini both fail or return invalid structured output."""


def _clean_json_text(raw: str) -> str:
    """Strip <think> blocks, markdown fences, and extract the JSON object cleanly."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    return cleaned


def get_model_label(provider: ProviderType) -> str:
    if provider == "ollama":
        return f"Ollama ({settings.OLLAMA_MODEL})"
    if provider == "gemini":
        return f"Gemini ({settings.GEMINI_MODEL})"
    return provider


def _get_gemini_candidates() -> List[str]:
    seen = set()
    candidates: List[str] = []
    for model_name in [
        settings.GEMINI_MODEL,
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-3.1-flash-lite",
    ]:
        if model_name and model_name not in seen:
            seen.add(model_name)
            candidates.append(model_name)
    return candidates


def _type_to_example(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (list, List):
        args = get_args(annotation)
        inner = _type_to_example(args[0]) if args else "string"
        return [inner]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {
            name: _type_to_example(info.annotation)
            for name, info in annotation.model_fields.items()
        }
    if annotation is int:
        return 0
    if annotation is float:
        return 1.5
    if annotation is bool:
        return True
    return "string"


def _compact_schema_hint(response_model: Type[BaseModel]) -> str:
    return json.dumps(_type_to_example(response_model))


async def _call_ollama_native(
    system_prompt: str,
    user_prompt: str,
    schema_hint: str,
    temperature: float,
) -> Tuple[str, str]:
    ollama_host = settings.OLLAMA_BASE_URL.removesuffix("/v1").rstrip("/")
    url = f"{ollama_host}/api/chat"
    enriched_system = (
        f"{system_prompt}\n\n"
        "CRITICAL: Output ONLY valid JSON matching this structure. "
        "Populate arrays with ALL requested items (do NOT stop after 1 item):\n"
        f"{schema_hint}"
    )
    payload = {
        "model": settings.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": enriched_system},
            {"role": "user", "content": user_prompt},
        ],
        "think": False,
        "format": "json",
        "stream": False,
        "keep_alive": "15m",
        "options": {
            "temperature": temperature,
            "num_ctx": 2048,
            "num_predict": 1200,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "{}"), f"Ollama ({settings.OLLAMA_MODEL})"
    except httpx.ConnectError:
        raise LLMRouterError(
            f"Cannot connect to Ollama at {ollama_host}. Start Ollama (`ollama serve`) or switch to Gemini Flash."
        )


async def _call_gemini(
    system_prompt: str,
    user_prompt: str,
    response_model: Type[T],
    temperature: float,
) -> Tuple[str, str]:
    if not settings.GEMINI_API_KEY or settings.GEMINI_API_KEY.startswith("your_"):
        raise LLMRouterError("GEMINI_API_KEY is missing in .env")

    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    candidates = _get_gemini_candidates()
    attempted: List[str] = []
    last_err: Exception | None = None

    for idx, model_name in enumerate(candidates):
        attempted.append(model_name)
        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_schema=response_model,
                ),
            )
            return (response.text or "{}"), f"Gemini ({model_name})"
        except Exception as err:
            last_err = err
            err_str = str(err)
            if any(code in err_str for code in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "404")):
                if idx < len(candidates) - 1:
                    await asyncio.sleep(1.0 * (idx + 1))
                    continue
            raise

    raise LLMRouterError(f"Gemini models busy ({', '.join(attempted)}): {last_err}")


async def generate_structured(
    provider: ProviderType,
    system_prompt: str,
    user_prompt: str,
    response_model: Type[T],
    temperature: float = 0.2,
    max_retries: int = 1,
) -> Tuple[T, str]:
    schema_hint = _compact_schema_hint(response_model)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            if provider == "ollama":
                raw_text, used_label = await _call_ollama_native(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    schema_hint=schema_hint,
                    temperature=temperature,
                )
            elif provider == "gemini":
                try:
                    raw_text, used_label = await _call_gemini(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        response_model=response_model,
                        temperature=temperature,
                    )
                except Exception as gemini_err:
                    if any(code in str(gemini_err) for code in ("503", "UNAVAILABLE", "429")):
                        raw_text, ollama_label = await _call_ollama_native(
                            system_prompt=system_prompt,
                            user_prompt=user_prompt,
                            schema_hint=schema_hint,
                            temperature=temperature,
                        )
                        used_label = f"{ollama_label} [Cloud 503 Failover]"
                    else:
                        raise
            else:
                raise LLMRouterError(f"Unsupported LLM provider: {provider}")

            cleaned = _clean_json_text(raw_text)
            parsed = response_model.model_validate_json(cleaned)
            return parsed, used_label

        except (ValidationError, json.JSONDecodeError) as e:
            last_error = e
            user_prompt += "\n\nReturn strict valid JSON matching the exact keys."
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                await asyncio.sleep(0.5)

    raise LLMRouterError(f"{last_error}")