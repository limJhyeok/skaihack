"""FastAPI 앱. 봉인 정보는 공개 단계 전까지 어떤 응답에도 넣지 않는다."""
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import json
from pathlib import Path

from . import cards, collector, criteria, dashboard, evaluate, fresh, generator, helper, persona, policy, reading, rehearsal, reveal, runner, sessions, submit, timer, workspace
from .config import APP_ROOT, settings
from .llm import LLMError
from .schemas import Difficulty, GeneratedProblem, ProblemPublic, Sealed, Session

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
        "archetypes": [{"id": k, "description": v["description"]} for k, v in generator.ARCHETYPES.items()],
        "difficulties": list(generator.DIFFICULTY),
    }


@app.post("/api/problems")
def new_problem(req: NewProblemRequest):
    if not settings.model_strong:
        raise HTTPException(500, "MOCK_MODEL_STRONG 환경변수가 설정되지 않았습니다.")
    try:
        choice = generator.pick(req.domain, req.archetype, req.difficulty, req.seed, sessions.used_domains(),
                                sessions.recent_archetypes(), sessions.recent_core_concepts())
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
    sealed = Sealed(**gen.model_dump(include=set(GeneratedProblem.model_fields) & set(Sealed.model_fields)),
                    ai_fit=choice["ai_fit"], tech_requirement=choice["tech_requirement"], themes=choice["themes"])
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


# ---------- 동작 추적 카드 드릴 (docs/drill_design.md) ----------

class DeckCreate(BaseModel):
    session_id: str | None = None
    repo_id: str | None = None   # 새 문제 저장소
    count: int = cards.DEFAULT_CARDS


class FreshCreate(BaseModel):
    count: int = cards.DEFAULT_CARDS
    seed: int | None = None


class RunCreate(BaseModel):
    mode: Literal["closed", "open"] = "closed"
    count: int = 10
    session_id: str | None = None  # 재료 키: 세션 ID 또는 새 문제 저장소 ID


class CardAnswer(BaseModel):
    answers: dict[str, str]
    timed_out: bool = False


@app.get("/api/drills/sources")
def drill_sources():
    return {"sources": cards.sources(), "decks": cards.deck_list(), "runs": cards.list_runs()}


@app.post("/api/drills/decks")
def create_deck(req: DeckCreate):
    if not 3 <= req.count <= 12:
        raise HTTPException(400, "카드 수는 3~12입니다.")
    if req.repo_id:
        try:
            fresh.load(req.repo_id)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "저장소를 찾을 수 없습니다.")
        deck = _card_call(lambda: fresh.build_deck(req.repo_id, req.count))
    elif req.session_id:
        _load(req.session_id)
        deck = _card_call(lambda: cards.generate_deck(req.session_id, req.count))
    else:
        raise HTTPException(400, "session_id 또는 repo_id가 필요합니다.")
    return {k: deck[k] for k in ("deck_id", "generation")}


@app.post("/api/drills/fresh")
def create_fresh(req: FreshCreate):
    if not 3 <= req.count <= 12:
        raise HTTPException(400, "카드 수는 3~12입니다.")
    try:
        return _card_call(lambda: fresh.create(req.count, req.seed))
    except fresh.FreshError as e:
        raise HTTPException(502, str(e))


@app.post("/api/drills/runs")
def create_run(req: RunCreate):
    if not 1 <= req.count <= 30:
        raise HTTPException(400, "카드 수는 1~30입니다.")
    return _card_call(lambda: cards.run_view(cards.create_run(req.mode, req.count, req.session_id)))


@app.get("/api/drills/runs/{run_id}")
def get_run(run_id: str):
    return cards.run_view(_load_run(run_id))


@app.post("/api/drills/runs/{run_id}/cards/{n}/start")
def start_card(run_id: str, n: int):
    _load_run(run_id)
    return _card_call(lambda: cards.start_item(run_id, n))


@app.post("/api/drills/runs/{run_id}/cards/{n}/answer")
def answer_card(run_id: str, n: int, req: CardAnswer):
    _load_run(run_id)
    return _card_call(lambda: cards.answer(run_id, n, req.answers, req.timed_out))


@app.get("/api/drills/runs/{run_id}/cards/{n}/result")
def card_result(run_id: str, n: int):
    run = _load_run(run_id)
    return _card_call(lambda: cards.result_view(run, cards._item(run, n)))


@app.post("/api/drills/runs/{run_id}/cards/{n}/flag")
def flag_card(run_id: str, n: int):
    _load_run(run_id)
    return _card_call(lambda: cards.flag(run_id, n))


def _load_run(run_id: str) -> dict:
    try:
        return cards.load_run(run_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "드릴을 찾을 수 없습니다.")


def _card_call(fn):
    try:
        return fn()
    except cards.CardError as e:
        raise HTTPException(409, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


# ---------- 정책 묶음 + B. 성공 기준 드릴 (docs/flow_drills_design.md 3·4절) ----------

class BundleCreate(BaseModel):
    seed: int | None = None


class SheetCreate(BaseModel):
    bundle_id: str
    seconds: int = criteria.DEFAULT_SECONDS   # 0이면 시간 제한 없음


class SheetRow(BaseModel):
    func: str
    args: dict[str, str]
    expected: str
    note: str = ""


class SheetRows(BaseModel):
    rows: list[SheetRow]
    timed_out: bool = False


@app.get("/api/criteria")
def criteria_overview():
    return criteria.overview()


@app.post("/api/criteria/bundles")
def create_bundle(req: BundleCreate):
    if not settings.model_strong:
        raise HTTPException(500, "MOCK_MODEL_STRONG 환경변수가 설정되지 않았습니다.")
    try:
        return policy.summary(policy.generate(req.seed))
    except policy.PolicyError as e:
        raise HTTPException(502, str(e))
    except LLMError as e:
        raise HTTPException(502, str(e))


@app.post("/api/criteria/sheets")
def create_sheet(req: SheetCreate):
    if not 0 <= req.seconds <= 7200:
        raise HTTPException(400, "시간은 0~7200초입니다.")
    try:
        sheet = criteria.create(req.bundle_id, req.seconds)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "정책 묶음을 찾을 수 없습니다.")
    return criteria.view(sheet)


@app.get("/api/criteria/sheets/{sheet_id}")
def get_sheet(sheet_id: str):
    return criteria.view(_load_sheet(sheet_id))


@app.put("/api/criteria/sheets/{sheet_id}/rows")
def save_sheet_rows(sheet_id: str, req: SheetRows):
    _load_sheet(sheet_id)
    return _criteria_call(lambda: criteria.save_rows(sheet_id, [r.model_dump() for r in req.rows]))


@app.post("/api/criteria/sheets/{sheet_id}/submit")
def submit_sheet(sheet_id: str, req: SheetRows):
    _load_sheet(sheet_id)
    return _criteria_call(lambda: criteria.submit(sheet_id, [r.model_dump() for r in req.rows], req.timed_out))


def _load_sheet(sheet_id: str) -> dict:
    try:
        return criteria.load(sheet_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "답안을 찾을 수 없습니다.")


def _criteria_call(fn):
    try:
        return fn()
    except criteria.CriteriaError as e:
        raise HTTPException(409, str(e))


# ---------- A. 읽기·정리 드릴 (docs/flow_drills_design.md 4절) ----------

class ReadCreate(BaseModel):
    bundle_id: str
    seconds: int = reading.DEFAULT_SECONDS   # 0이면 시간 제한 없음


class ReadText(BaseModel):
    text: str
    timed_out: bool = False


@app.get("/api/reading")
def reading_overview():
    return reading.overview()


@app.post("/api/reading/sheets")
def create_read(req: ReadCreate):
    if not 0 <= req.seconds <= 7200:
        raise HTTPException(400, "시간은 0~7200초입니다.")
    try:
        sheet = reading.create(req.bundle_id, req.seconds)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "정책 묶음을 찾을 수 없습니다.")
    return reading.view(sheet)


@app.get("/api/reading/sheets/{read_id}")
def get_read(read_id: str):
    return reading.view(_load_read(read_id))


@app.put("/api/reading/sheets/{read_id}/text")
def save_read_text(read_id: str, req: ReadText):
    _load_read(read_id)
    return _reading_call(lambda: reading.save_text(read_id, req.text))


@app.post("/api/reading/sheets/{read_id}/submit")
def submit_read(read_id: str, req: ReadText):
    _load_read(read_id)
    if not settings.model_strong:
        raise HTTPException(500, "MOCK_MODEL_STRONG 환경변수가 설정되지 않았습니다.")
    return _reading_call(lambda: reading.submit(read_id, req.text, req.timed_out))


def _load_read(read_id: str) -> dict:
    try:
        return reading.load(read_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "답안을 찾을 수 없습니다.")


def _reading_call(fn):
    try:
        return fn()
    except reading.ReadingError as e:
        raise HTTPException(409, str(e))
    except LLMError as e:
        raise HTTPException(502, f"채점 실패 (답안은 저장됨, 다시 제출하세요): {e}")


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
