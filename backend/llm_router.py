import json
import os
import re
from typing import Tuple, Type, TypeVar

import httpx
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMRouterError(Exception):
    """Raised when the configured LLM provider fails or returns invalid structured output."""


def get_model_label(provider: str) -> str:
    clean = (provider or "gemini").strip().lower()
    if clean == "ollama":
        model = (os.getenv("OLLAMA_MODEL") or "qwen2.5:7b").strip()
        return f"Ollama ({model})"
    model = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()
    return f"Gemini ({model})"


def _extract_json_block(raw_text: str) -> str:
    text = (raw_text or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


async def _call_gemini_structured(
    system_prompt: str,
    user_prompt: str,
    response_schema: Type[T],
) -> Tuple[T, str]:
    api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        raise LLMRouterError("GEMINI_API_KEY is not configured in environment variables.")

    model_name = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = await client.aio.models.generate_content(
            model=model_name,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                response_schema=response_schema,
                temperature=0.3,
            ),
        )
        raw = response.text or ""
        parsed = response_schema.model_validate_json(_extract_json_block(raw))
        return parsed, f"Gemini ({model_name})"
    except ImportError:
        # Fallback to direct Gemini REST API if google-genai SDK is not installed
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        schema_json = json.dumps(response_schema.model_json_schema(), indent=2)
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": f"{user_prompt}\n\nReturn ONLY valid JSON matching this schema:\n{schema_json}"
                        }
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.3,
            },
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                raise LLMRouterError(f"Gemini API error ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = response_schema.model_validate_json(_extract_json_block(raw))
            return parsed, f"Gemini ({model_name})"
    except LLMRouterError:
        raise
    except Exception as err:
        raise LLMRouterError(f"Gemini generation failed: {err}") from err


async def _call_ollama_structured(
    system_prompt: str,
    user_prompt: str,
    response_schema: Type[T],
) -> Tuple[T, str]:
    base_url = (os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")
    model_name = (os.getenv("OLLAMA_MODEL") or "qwen2.5:7b").strip()
    schema_json = json.dumps(response_schema.model_json_schema(), indent=2)

    payload = {
        "model": model_name,
        "format": "json",
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": f"{system_prompt}\nYou must respond strictly with valid JSON matching this schema:\n{schema_json}",
            },
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": 0.3},
    }

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(f"{base_url}/api/chat", json=payload)
            if resp.status_code != 200:
                raise LLMRouterError(f"Ollama error ({resp.status_code}): {resp.text[:300]}")
            data = resp.json()
            raw = data.get("message", {}).get("content", "")
            parsed = response_schema.model_validate_json(_extract_json_block(raw))
            return parsed, f"Ollama ({model_name})"
    except LLMRouterError:
        raise
    except Exception as err:
        raise LLMRouterError(f"Ollama connection failed ({base_url}): {err}") from err


async def call_structured_llm(
    provider: str,
    system_prompt: str,
    user_prompt: str,
    response_schema: Type[T],
) -> Tuple[T, str]:
    clean = (provider or "gemini").strip().lower()
    if clean == "ollama":
        return await _call_ollama_structured(system_prompt, user_prompt, response_schema)
    return await _call_gemini_structured(system_prompt, user_prompt, response_schema)