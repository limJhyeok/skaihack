"""세션 디렉터리(data/sessions/<id>/) 읽기·쓰기. 봉인 정보는 sealed.bin에 암호화 저장."""
import json
import secrets
from datetime import datetime
from pathlib import Path

from . import seal
from .config import settings
from .schemas import ProblemPublic, Sealed, Session


def sessions_dir() -> Path:
    d = settings.data_dir / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_ids() -> tuple[str, str]:
    now = datetime.now()
    tag = secrets.token_hex(2)
    return f"s_{now:%Y%m%d_%H%M%S}_{tag}", f"p_{now:%Y%m%d}_{tag}"


def session_dir(session_id: str) -> Path:
    d = sessions_dir() / session_id
    if d.parent != sessions_dir():  # 경로 조작 방지
        raise ValueError("잘못된 세션 ID")
    return d


def save_new(session: Session, problem: ProblemPublic, sealed: Sealed) -> None:
    d = session_dir(session.session_id)
    d.mkdir(parents=True)
    save_session(session)
    (d / "problem_public.json").write_text(problem.model_dump_json(indent=2), encoding="utf-8")
    (d / "sealed.bin").write_bytes(seal.seal(sealed.model_dump_json()))


def save_session(session: Session) -> None:
    (session_dir(session.session_id) / "session.json").write_text(session.model_dump_json(indent=2), encoding="utf-8")


def load_session(session_id: str) -> Session:
    return Session.model_validate_json((session_dir(session_id) / "session.json").read_text(encoding="utf-8"))


def load_problem(session_id: str) -> ProblemPublic:
    return ProblemPublic.model_validate_json((session_dir(session_id) / "problem_public.json").read_text(encoding="utf-8"))


def load_sealed(session_id: str) -> Sealed:
    """평가·공개 단계 전용. 응시 중 API 응답에 쓰지 않는다."""
    return Sealed.model_validate_json(seal.unseal((session_dir(session_id) / "sealed.bin").read_bytes()))


def used_domains() -> set[str]:
    out = set()
    for f in sessions_dir().glob("*/problem_public.json"):
        out.add(json.loads(f.read_text(encoding="utf-8"))["domain"])
    return out


# ---------- JSONL 기록 (chat.jsonl, helper_chat.jsonl, events.jsonl) ----------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def elapsed_sec(session: Session, ts: str | None = None) -> int:
    if not session.started_at:
        return 0
    t = datetime.fromisoformat(ts) if ts else datetime.now().astimezone()
    return max(0, int((t - datetime.fromisoformat(session.started_at)).total_seconds()))


def append_jsonl(session_id: str, name: str, record: dict) -> None:
    with open(session_dir(session_id) / name, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(session_id: str, name: str) -> list[dict]:
    p = session_dir(session_id) / name
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def list_sessions() -> list[Session]:
    out = []
    for f in sorted(sessions_dir().glob("*/session.json"), reverse=True):
        out.append(Session.model_validate_json(f.read_text(encoding="utf-8")))
    return out
