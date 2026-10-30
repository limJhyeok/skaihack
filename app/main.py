"""FastAPI 앱. 봉인 정보는 공개 단계 전까지 어떤 응답에도 넣지 않는다."""
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import json
from pathlib import Path

from . import collector, dashboard, evaluate, generator, helper, persona, rehearsal, reveal, runner, sessions, submit, timer, workspace
from .config import APP_ROOT, settings
from .llm import LLMError
from .schemas import Difficulty, ProblemPublic, Sealed, Session

app = FastAPI(title="mockdrill")


class NewProblemRequest(BaseModel):
    domain: str | None = None
    archetype: str | None = None
    difficulty: Difficulty = "standard"
    seed: int | None = None


@app.get("/api/options")
def options():
    return {
        "domains": generator.DOMAINS,
        "archetypes": [{"id": k, "description": v} for k, v in generator.ARCHETYPES.items()],
        "difficulties": list(generator.DIFFICULTY),
    }


@app.post("/api/problems")
def new_problem(req: NewProblemRequest):
    if not settings.model_strong:
        raise HTTPException(500, "MOCK_MODEL_STRONG 환경변수가 설정되지 않았습니다.")
    try:
        choice = generator.pick(req.domain, req.archetype, req.difficulty, req.seed, sessions.used_domains())
        gen = generator.generate(choice)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except (LLMError, generator.GenerationError) as e:
        raise HTTPException(502, str(e))

    session_id, problem_id = sessions.new_ids()
    problem = ProblemPublic(
        problem_id=problem_id, title=gen.title, domain=choice["domain"], archetype=choice["archetype"],
        difficulty=choice["difficulty"], readme_markdown=gen.readme_markdown,
        data_files=gen.data_files, persona_public=gen.persona_public,
    )
    sealed = Sealed(**gen.model_dump(include=set(Sealed.model_fields)))
    ws = workspace.create(session_id, problem)
    session = Session(session_id=session_id, problem_id=problem_id, workspace_path=str(ws),
                      duration_min=settings.duration_min)
    sessions.save_new(session, problem, sealed)
    return _summary(session, problem)


@app.get("/api/sessions")
def list_sessions():
    return [_summary(s, sessions.load_problem(s.session_id)) for s in sessions.list_sessions()]


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    s = _load(session_id)
    return {**_summary(s, sessions.load_problem(session_id)), "readme_markdown": sessions.load_problem(session_id).readme_markdown}


class StartRequest(BaseModel):
    mode: Literal["full", "practice"] = "full"


@app.post("/api/sessions/{session_id}/start")
def start(session_id: str, req: StartRequest):
    s = _load(session_id)
    if s.status == "created":
        s.mode, s.status, s.started_at = req.mode, "running", sessions.now_iso()
        sessions.save_session(s)
        timer.log_event(s, "start", mode=s.mode)
        persona.opening(session_id)
    return _summary(s, sessions.load_problem(session_id))


class ChatRequest(BaseModel):
    text: str


@app.get("/api/sessions/{session_id}/chat")
def chat_log(session_id: str):
    _load(session_id)
    return {"messages": persona.public_log(session_id), "question_count": persona.question_count(session_id)}


@app.post("/api/sessions/{session_id}/chat")
def chat_send(session_id: str, req: ChatRequest):
    _require_running(session_id)
    try:
        reply = persona.send(session_id, _text(req))
    except LLMError as e:
        raise HTTPException(502, f"담당자 응답 실패. 메시지는 저장되었으니 재전송하세요. ({e})")
    return {"reply": reply, "question_count": persona.question_count(session_id)}


@app.get("/api/sessions/{session_id}/helper")
def helper_log(session_id: str):
    _load(session_id)
    return {"messages": helper.log(session_id)}


@app.post("/api/sessions/{session_id}/helper")
def helper_send(session_id: str, req: ChatRequest):
    _load(session_id)
    try:
        return {"reply": helper.send(session_id, _text(req))}
    except LLMError as e:
        raise HTTPException(502, f"Helper 응답 실패. 재전송하세요. ({e})")


# ---------- 타이머 ----------

@app.get("/api/sessions/{session_id}/timer")
def get_timer(session_id: str):
    return timer.state(_load(session_id))


@app.post("/api/sessions/{session_id}/pause")
def pause(session_id: str):
    s = _require_practice(session_id)
    if not timer.state(s)["paused"]:
        timer.log_event(s, "pause")
    return timer.state(s)


@app.post("/api/sessions/{session_id}/resume")
def resume(session_id: str):
    s = _require_practice(session_id)
    if timer.state(s)["paused"]:
        timer.log_event(s, "resume")
    return timer.state(s)


class AlertShown(BaseModel):
    at_min: int


@app.post("/api/sessions/{session_id}/alert")
def alert_shown(session_id: str, req: AlertShown):
    timer.log_event(_load(session_id), "alert", at_min=req.at_min)
    return {"ok": True}


def _require_practice(session_id: str) -> Session:
    s = _load(session_id)
    if s.mode != "practice":
        raise HTTPException(409, "일시정지는 연습 모드에서만 가능합니다.")
    if s.status != "running":
        raise HTTPException(409, "응시 중인 세션이 아닙니다.")
    return s


# ---------- 제출 ----------

@app.get("/api/sessions/{session_id}/submit-info")
def submit_info(session_id: str):
    s = _load(session_id)
    ws = Path(s.workspace_path)
    cmd, port = submit.readme_command(ws)
    return {"readme_command": cmd, "readme_port": port, "dirty_files": submit.dirty_files(ws),
            "status": s.status}


class SubmitRequest(BaseModel):
    agent_tool: Literal["codex", "claude_code", "other"] = "codex"
    auto_commit: bool = True
    manual_tokens: int | None = None


@app.post("/api/sessions/{session_id}/submit")
def do_submit(session_id: str, req: SubmitRequest):
    s = _load(session_id)
    if s.status != "running":
        raise HTTPException(409, "응시 중인 세션만 제출할 수 있습니다.")
    if s.mode == "practice" and timer.state(s)["paused"]:
        timer.log_event(s, "resume")
    return submit.submit(s, req.agent_tool, req.auto_commit, req.manual_tokens)


@app.get("/api/sessions/{session_id}/submission")
def get_submission(session_id: str):
    _load(session_id)
    p = sessions.session_dir(session_id) / "submission.json"
    if not p.exists():
        raise HTTPException(404, "제출 기록이 없습니다.")
    return json.loads(p.read_text(encoding="utf-8"))


# ---------- 설명 리허설 ----------

class RehearsalStart(BaseModel):
    mode: Literal["closed_book", "open_book"] = "closed_book"
    no_time_limit: bool = False  # open_book에서만 허용


@app.post("/api/sessions/{session_id}/rehearsal")
def start_session_rehearsal(session_id: str, req: RehearsalStart):
    s = _load(session_id)
    if rehearsal.exists(session_id):
        return rehearsal.public_view(rehearsal.load(session_id))
    if s.status != "submitted":
        raise HTTPException(409, "제출한 세션에서만 리허설을 시작할 수 있습니다.")
    seconds = 0 if (req.mode == "open_book" and req.no_time_limit) else settings.rehearsal_sec
    return _rehearsal_call(lambda: rehearsal.public_view(rehearsal.create_for_session(session_id, req.mode, seconds)))


class StandaloneStart(BaseModel):
    repo_path: str
    purpose: str = ""
    mode: Literal["closed_book", "open_book"] = "closed_book"
    count: int = settings.rehearsal_count
    seconds: int = settings.rehearsal_sec
    no_time_limit: bool = False


@app.post("/api/rehearsals")
def start_standalone(req: StandaloneStart):
    if not 1 <= req.count <= 10:
        raise HTTPException(400, "문항 수는 1~10입니다.")
    seconds = 0 if (req.mode == "open_book" and req.no_time_limit) else max(30, req.seconds)
    return _rehearsal_call(lambda: rehearsal.public_view(
        rehearsal.create_standalone(req.repo_path, req.purpose, req.mode, req.count, seconds)))


@app.get("/api/rehearsals")
def list_rehearsals():
    return rehearsal.list_standalone()


@app.get("/api/rehearsals/{rid}")
def get_rehearsal(rid: str):
    return rehearsal.public_view(_load_rehearsal(rid))


@app.post("/api/rehearsals/{rid}/next")
def next_question(rid: str):
    _load_rehearsal(rid)
    return {"question": rehearsal.next_question(rid)}


class AnswerRequest(BaseModel):
    answer: str
    timed_out: bool = False


@app.post("/api/rehearsals/{rid}/questions/{qid}/answer")
def answer_question(rid: str, qid: str, req: AnswerRequest):
    _load_rehearsal(rid)
    return _rehearsal_call(lambda: rehearsal.answer(rid, qid, req.answer, req.timed_out))


@app.post("/api/rehearsals/{rid}/grade")
def grade_rehearsal(rid: str):
    _load_rehearsal(rid)
    return _rehearsal_call(lambda: rehearsal.public_view(rehearsal.grade(rid)))


@app.post("/api/rehearsals/{rid}/followup")
def followup(rid: str):
    _load_rehearsal(rid)
    return _rehearsal_call(lambda: rehearsal.create_followup(rid))


def _load_rehearsal(rid: str) -> dict:
    try:
        return rehearsal.load(rid)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "리허설을 찾을 수 없습니다.")


def _rehearsal_call(fn):
    try:
        return fn()
    except rehearsal.RehearsalError as e:
        raise HTTPException(409, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


# ---------- 평가 리포트 / 정답 공개 ----------

@app.post("/api/sessions/{session_id}/evaluate")
def run_evaluation(session_id: str):
    _load(session_id)
    try:
        return evaluate.evaluate(session_id)
    except (evaluate.EvaluationError, runner.PortBusy) as e:
        raise HTTPException(409, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.get("/api/sessions/{session_id}/report")
def get_report(session_id: str):
    _load(session_id)
    r = evaluate.load_report(session_id)
    if r is None:
        raise HTTPException(404, "리포트가 없습니다.")
    return r


@app.post("/api/sessions/{session_id}/reveal")
def do_reveal(session_id: str):
    _load(session_id)
    try:
        return reveal.reveal(session_id)
    except reveal.RevealError as e:
        raise HTTPException(409, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.get("/api/sessions/{session_id}/reveal")
def get_reveal(session_id: str):
    s = _load(session_id)
    if s.status != "revealed":
        raise HTTPException(409, "아직 공개되지 않았습니다.")
    return reveal.load(session_id)


class RetroRequest(BaseModel):
    text: str


@app.put("/api/sessions/{session_id}/retro")
def put_retro(session_id: str, req: RetroRequest):
    s = _load(session_id)
    if s.status != "revealed":
        raise HTTPException(409, "공개 후에 회고를 저장할 수 있습니다.")
    reveal.save_retro(session_id, req.text)
    return {"ok": True}


# ---------- 기록 대시보드 ----------

@app.get("/api/dashboard")
def get_dashboard():
    return dashboard.build()


# ---------- 설정 (에이전트 로그 경로) ----------

class LogPaths(BaseModel):
    codex_sessions_dir: str = ""
    claude_projects_dir: str = ""


@app.get("/api/settings")
def get_settings():
    return collector.log_paths()


@app.put("/api/settings")
def put_settings(req: LogPaths):
    return collector.save_log_paths(req.model_dump())


def _load(session_id: str) -> Session:
    try:
        return sessions.load_session(session_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "세션을 찾을 수 없습니다.")


def _require_running(session_id: str) -> Session:
    s = _load(session_id)
    if s.status != "running":
        raise HTTPException(409, "응시 중인 세션에서만 담당자와 대화할 수 있습니다.")
    t = timer.state(s)
    if t["time_over"]:
        raise HTTPException(409, "시간이 종료되었습니다. 제출 화면으로 이동하세요.")
    if t["paused"]:
        raise HTTPException(409, "일시정지 중입니다.")
    return s


def _text(req: ChatRequest) -> str:
    t = req.text.strip()
    if not t:
        raise HTTPException(400, "빈 메시지입니다.")
    return t


def _summary(session: Session, problem: ProblemPublic) -> dict:
    """출제 화면 노출 규칙(5.1): 제목·도메인·유형·난이도·작업공간 경로만."""
    return {
        "session_id": session.session_id,
        "status": session.status,
        "mode": session.mode,
        "started_at": session.started_at,
        "duration_min": session.duration_min,
        "title": problem.title,
        "domain": problem.domain,
        "archetype": problem.archetype,
        "difficulty": problem.difficulty,
        "workspace_path": session.workspace_path,
        "persona": problem.persona_public.model_dump(),
        "has_rehearsal": rehearsal.exists(session.session_id),
        "has_report": evaluate.load_report(session.session_id) is not None,
    }


app.mount("/", StaticFiles(directory=APP_ROOT / "web", html=True), name="web")
