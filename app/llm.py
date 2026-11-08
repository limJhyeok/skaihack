"""OpenAI Structured Outputs 래퍼. 실패 시 1회 재시도, 모든 호출의 토큰 사용량을 data/usage.jsonl에 기록."""
import json
import logging
import time
from datetime import datetime, timezone
from typing import TypeVar

import httpx
from openai import OpenAI
from pydantic import BaseModel

from .config import settings

T = TypeVar("T", bound=BaseModel)
_client: OpenAI | None = None
log = logging.getLogger("uvicorn.error")

# 스트리밍이라 read 타임아웃은 "청크 사이 무응답 시간"이다. 추론 모델은 첫 토큰 전에 오래 생각할 수 있어 넉넉히 둔다.
TIMEOUT = httpx.Timeout(600.0, connect=15.0)
PROGRESS_EVERY_CHARS = 4000


class LLMError(RuntimeError):
    pass


def client() -> OpenAI:
    global _client
    if _client is None:
        # OPENAI_API_KEY / OPENAI_BASE_URL은 SDK가 환경변수에서 직접 읽는다.
        # SDK 자체 재시도는 끈다: 타임아웃 재시도가 parse()의 재시도와 곱해져 최대 30분까지 멈춰 보였다.
        _client = OpenAI(timeout=TIMEOUT, max_retries=0)
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


def _stream_parse(*, model: str, system: str, user: str, schema: type[T], purpose: str):
    """스트리밍으로 받아 진행 상황을 서버 로그에 남긴다. 결과는 parse()와 같은 ParsedChatCompletion."""
    start = time.monotonic()
    log.info("LLM 시작(%s, %s)", purpose, model)
    received, next_mark = 0, PROGRESS_EVERY_CHARS
    with client().chat.completions.stream(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format=schema,
        stream_options={"include_usage": True},
    ) as stream:
        for event in stream:
            if event.type == "content.delta":
                received += len(event.delta)
                if received >= next_mark:
                    log.info("LLM 진행(%s): %d자 수신, %.0f초 경과", purpose, received, time.monotonic() - start)
                    next_mark += PROGRESS_EVERY_CHARS
        r = stream.get_final_completion()
    log.info("LLM 완료(%s): %d자, %.0f초", purpose, received, time.monotonic() - start)
    return r


def parse(*, model: str, system: str, user: str, schema: type[T], purpose: str) -> T:
    last: Exception | None = None
    for _ in range(2):
        try:
            r = _stream_parse(model=model, system=system, user=user, schema=schema, purpose=purpose)
            if r.usage:
                _log_usage(purpose, model, r.usage)
            parsed = r.choices[0].message.parsed
            if parsed is None:
                raise LLMError(f"응답이 스키마를 따르지 않음: {r.choices[0].message.refusal}")
            return parsed
        except Exception as e:  # 타임아웃, 스키마 불일치 등
            log.warning("LLM 실패(%s): %s: %s", purpose, type(e).__name__, e)
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
