import json
from pathlib import Path

from app import sessions
from app.config import settings
from app.schemas import ProblemPublic, Sealed, Session

FIXTURE = Path(__file__).parent / "fixtures" / "llm_problem.json"


def load_fixture() -> tuple[ProblemPublic, Sealed]:
    d = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return ProblemPublic(**d["public"]), Sealed(**d["sealed"])


def make_session(status: str = "running") -> str:
    """픽스처 문제로 세션을 만든다 (작업공간 생성·LLM 호출 없음)."""
    problem, sealed = load_fixture()
    sid, _ = sessions.new_ids()
    s = Session(session_id=sid, problem_id=problem.problem_id, workspace_path="/tmp/ws_fixture", duration_min=settings.duration_min,
                status="created")
    sessions.save_new(s, problem, sealed)
    if status == "running":
        s.status, s.started_at = "running", sessions.now_iso()
        sessions.save_session(s)
    return sid
