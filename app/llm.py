"""OpenAI Structured Outputs 래퍼. 실패 시 1회 재시도, 모든 호출의 토큰 사용량을 data/usage.jsonl에 기록."""
import json
from datetime import datetime, timezone
from typing import TypeVar

from openai import OpenAI
from pydantic import BaseModel

from .config import settings

T = TypeVar("T", bound=BaseModel)
_client: OpenAI | None = None


class LLMError(RuntimeError):
    pass


def client() -> OpenAI:
    global _client
    if _client is None:
        # OPENAI_API_KEY / OPENAI_BASE_URL은 SDK가 환경변수에서 직접 읽는다.
        _client = OpenAI(timeout=300)
    return _client


def _log_usage(purpose: str, model: str, usage) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "purpose": purpose,
        "model": model,
        "input": getattr(usage, "prompt_tokens", None),
        "output": getattr(usage, "completion_tokens", None),
        "total": getattr(usage, "total_tokens", None),
    }
    with open(settings.data_dir / "usage.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def parse(*, model: str, system: str, user: str, schema: type[T], purpose: str) -> T:
    last: Exception | None = None
    for _ in range(2):
        try:
            r = client().chat.completions.parse(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=schema,
            )
            if r.usage:
                _log_usage(purpose, model, r.usage)
            parsed = r.choices[0].message.parsed
            if parsed is None:
                raise LLMError(f"응답이 스키마를 따르지 않음: {r.choices[0].message.refusal}")
            return parsed
        except Exception as e:  # 타임아웃, 스키마 불일치 등
            last = e
    raise LLMError(f"LLM 호출 실패({purpose}): {type(last).__name__}: {last}")


def chat(*, model: str, messages: list[dict], purpose: str) -> str:
    """자유 텍스트 응답 (페르소나·Helper). 실패 시 1회 재시도."""
    last: Exception | None = None
    for _ in range(2):
        try:
            r = client().chat.completions.create(model=model, messages=messages)
            if r.usage:
                _log_usage(purpose, model, r.usage)
            text = (r.choices[0].message.content or "").strip()
            if not text:
                raise LLMError("빈 응답")
            return text
        except Exception as e:
            last = e
    raise LLMError(f"LLM 호출 실패({purpose}): {type(last).__name__}: {last}")
