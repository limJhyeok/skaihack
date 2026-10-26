"""타이머와 단계 알림 (5.5). 남은 시간은 events.jsonl의 start/pause/resume으로 서버에서 계산한다."""
from datetime import datetime

from . import sessions
from .schemas import Session

EVENTS = "events.jsonl"

# (분, 문구). 실제 시험 작업 시간 180분 기준. 가이드는 종료 10분 전 제출 시작을 권장.
ALERTS = [
    (15, "README와 환경 확인을 마쳤나요? Helper에게 제출 흐름을 확인했나요?"),
    (45, "담당자와 서면 합의를 마치고 ACCEPTANCE.md를 썼나요?"),
    (110, "end-to-end 동작이 되나요? 안 되면 기능 추가를 멈추세요. HANDOFF.md를 갱신하고 세션 전환을 고려하세요."),
    (150, "새 기능 금지. 검증, 교차 검증, 문서-코드 일치 확인 시간입니다."),
    (170, "지금 제출을 시작하세요. 제출 후 확인 문제 6문항(문항당 5분, AI·붙여넣기 금지)이 있습니다. RISKS.md를 다시 읽으세요."),
]


def _paused_total(events: list[dict], now: datetime) -> tuple[float, bool]:
    total, paused_at = 0.0, None
    for e in events:
        if e["type"] == "pause" and paused_at is None:
            paused_at = datetime.fromisoformat(e["ts"])
        elif e["type"] == "resume" and paused_at is not None:
            total += (datetime.fromisoformat(e["ts"]) - paused_at).total_seconds()
            paused_at = None
    if paused_at is not None:
        total += (now - paused_at).total_seconds()
    return total, paused_at is not None


def state(session: Session, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    if not session.started_at:
        return {"started": False, "remaining_sec": session.duration_min * 60, "active_elapsed_sec": 0, "paused": False,
                "time_over": False, "mode": session.mode, "alerts": _alerts()}
    events = sessions.read_jsonl(session.session_id, EVENTS)
    paused_total, paused = _paused_total(events, now)
    active = (now - datetime.fromisoformat(session.started_at)).total_seconds() - paused_total
    remaining = session.duration_min * 60 - active
    return {
        "started": True,
        "remaining_sec": max(0, int(remaining)),
        "active_elapsed_sec": int(active),
        "paused": paused,
        "time_over": remaining <= 0,
        "mode": session.mode,
        "alerts": _alerts(),
    }


def _alerts() -> list[dict]:
    return [{"at_min": m, "text": t} for m, t in ALERTS]


def log_event(session: Session, type_: str, **extra) -> dict:
    rec = {"ts": sessions.now_iso(), "elapsed_sec": sessions.elapsed_sec(session), "type": type_, **extra}
    sessions.append_jsonl(session.session_id, EVENTS, rec)
    return rec
