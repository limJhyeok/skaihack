"""설명 리허설 (5.8, 5.11): 출제 → 한 문항씩 문답 → 채점 → 인지 축 집계.

저장 위치: 모의고사는 data/sessions/<session_id>/rehearsal.json, 단독 모드는 data/rehearsals/<rid>/rehearsal.json.
모범 답안은 채점 전까지 public_view()에 넣지 않는다.
"""
import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import codectx, llm, prompts, sessions
from .config import settings
from .schemas import GeneratedQuestions, GradeOutput, RehearsalQuestion

MAX_ATTEMPTS = 3
GRACE_SEC = 10  # 네트워크 지연 허용
STEP_SCORE = {"met": 1.0, "partial": 0.5, "missed": 0.0}
CATEGORIES = {"behavior_risk": "동작 및 리스크 예측", "structure": "구조 이해", "decision_rationale": "의사결정 및 근거 이해"}
EXPERIMENT_HINT = re.compile(r"(eval|experiment|result|benchmark|ablation|metric)", re.I)
COORD = re.compile(r"\S+:\d+")
DOC_SUFFIXES = {".md", ".txt", ".rst", ""}  # 문서 이름(README 등)은 요구사항 문항에서 쓸 수 있다


class RehearsalError(RuntimeError):
    pass


# ---------- 저장 ----------

def _dir(rid: str) -> Path:
    if rid.startswith("s_"):
        return sessions.session_dir(rid)
    d = settings.data_dir / "rehearsals" / rid
    if d.parent != settings.data_dir / "rehearsals":
        raise ValueError("잘못된 리허설 ID")
    return d


def load(rid: str) -> dict:
    p = _dir(rid) / "rehearsal.json"
    if not p.exists():
        raise FileNotFoundError(rid)
    return json.loads(p.read_text(encoding="utf-8"))


def save(r: dict) -> None:
    d = _dir(r["rehearsal_id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "rehearsal.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")


def exists(rid: str) -> bool:
    return (_dir(rid) / "rehearsal.json").exists()


# ---------- 출제 입력 ----------

def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n…(생략)"


def _session_inputs(session_id: str) -> tuple[codectx.CodeContext, dict]:
    d = sessions.session_dir(session_id)
    root = codectx.extract_snapshot(d / "snapshot.tar.gz", d / "snapshot")
    ctx = codectx.build(root)
    problem = sessions.load_problem(session_id)
    process = json.loads((d / "process.json").read_text(encoding="utf-8")) if (d / "process.json").exists() else {}
    docs = process.get("docs", {})
    scope = "\n\n".join(f"## {k}\n{docs[k]}" for k in ("SCOPE.md", "ACCEPTANCE.md") if k in docs) or "(문서 없음)"
    other = "\n\n".join(f"## {k}\n{v}" for k, v in docs.items() if k not in ("SCOPE.md", "ACCEPTANCE.md")) or "(문서 없음)"
    chat = "\n".join(f"[{r['role']}] {_cap(r['text'], 400)}" for r in sessions.read_jsonl(session_id, "chat.jsonl"))
    return ctx, {
        "readme": problem.readme_markdown,
        "scope_and_acceptance": _cap(scope, 6000),
        "docs": _cap(other, 8000),
        "chat": _cap(chat or "(대화 없음)", 8000),
        "agent_log_summary": _cap(agent_summary(process), 10000),
    }


def agent_summary(process: dict) -> str:
    traces = process.get("agent_traces") or []
    if not traces:
        return "(수집 안 됨)"
    lines = []
    for i, t in enumerate(traces, 1):
        lines.append(f"### 세션 {i} ({t['tool']}, 시작 +{t.get('elapsed_start_sec', '?')}초)")
        events = [("사용자", x) for x in t["user_prompts"]] + [("AI", x) for x in t.get("agent_messages", [])]
        events.sort(key=lambda e: e[1].get("ts") or "")
        for who, x in events:
            lines.append(f"- [{who}] {_cap(x['text'], 300 if who == 'AI' else 500)}")
        if t.get("files_edited"):
            lines.append("- 편집 파일: " + ", ".join(Path(f).name for f in t["files_edited"]))
    return "\n".join(lines)


def _standalone_inputs(repo: Path, purpose: str) -> tuple[codectx.CodeContext, dict]:
    ctx = codectx.build(repo)
    readme = next((ctx.read(n) for n in ("README.md", "readme.md", "README.rst", "README") if ctx.read(n)), None)
    return ctx, {
        "readme": _cap(readme or "(README 없음)", 6000),
        "scope_and_acceptance": purpose.strip() or "(입력 없음)",
        "docs": "(없음)",
        "chat": "(단독 리허설: 담당자 대화 없음)",
        "agent_log_summary": "(단독 리허설: 수집하지 않음)",
    }


# ---------- 출제와 검증 ----------

def _code_names(ctx: codectx.CodeContext) -> set[str]:
    """질문에 나오면 안 되는 이름: 함수·클래스·경로·코드 파일명. 문서 파일 이름은 뺀다."""
    docs = {n for rel in ctx.files for n in (Path(rel).name, Path(rel).stem) if Path(rel).suffix.lower() in DOC_SUFFIXES}
    return ctx.identifiers() - docs


def leaked_identifiers(text: str, names: set[str]) -> list[str]:
    """영숫자 경계로 감싼 완전한 토큰만 센다 (한글 조사가 붙어도 잡히고, 영단어 일부는 안 잡힌다)."""
    return sorted(n for n in names if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", text))


def validate(questions: list[RehearsalQuestion], count: int, ctx: codectx.CodeContext) -> list[str]:
    errors = []
    if len(questions) != count:
        errors.append(f"문항 수 {len(questions)}개 (요구 {count}개)")
    types = {q.type for q in questions}
    for need in ("design_reason", "failure_response"):
        if need not in types:
            errors.append(f"{need} 유형 문항이 없음")
    names = _code_names(ctx)
    for q in questions:
        leaked = leaked_identifiers(q.question, names)
        if leaked:
            errors.append(f"{q.id}: 질문에 코드 식별자({', '.join(leaked)})가 있음. 이름 대신 역할·동작으로 가리킬 것")
        for step in ("weak_point", "impact", "plan"):
            s = getattr(q.model_answer, step)
            if not s.text.strip() or not any(COORD.search(e) for e in s.evidence):
                errors.append(f"{q.id}: 모범 답안 {step}에 내용 또는 근거 좌표(파일:줄)가 없음")
    return errors


def _generate(ctx: codectx.CodeContext, inputs: dict, count: int, extra_rules: str) -> list[RehearsalQuestion]:
    feedback, errors = "", []
    for _ in range(MAX_ATTEMPTS):
        system = prompts.load("rehearsal_generate", count=count, file_tree=ctx.tree_text(),
                              key_files=ctx.key_files_text(), extra_rules=extra_rules + feedback, **inputs)
        out = llm.parse(model=settings.model_strong, system=system, user="확인 문제를 출제하라.",
                        schema=GeneratedQuestions, purpose="rehearsal_generate")
        errors = validate(out.questions, count, ctx)
        if not errors:
            return out.questions
        feedback = "\n[이전 출제가 검증에 실패한 이유 — 이번에는 반드시 고칠 것]\n" + "\n".join(f"- {e}" for e in errors) + "\n"
    raise RehearsalError("리허설 출제 검증 실패 (재출제 2회 후): " + "; ".join(errors))


def _new_record(rid: str, kind: str, mode: str, count: int, seconds: int, **extra) -> dict:
    return {"rehearsal_id": rid, "kind": kind, "mode": mode, "count": count, "seconds": seconds,
            "created_at": sessions.now_iso(), "status": "in_progress", "questions": [], "followups": [],
            "scores": None, **extra}


def _q_record(q: RehearsalQuestion, followup: bool = False) -> dict:
    return {**q.model_dump(), "followup": followup, "started_at": None, "answered_at": None,
            "answer": None, "elapsed_sec": None, "timed_out": False, "late": False, "grade": None}


def create_for_session(session_id: str, mode: str, seconds: int | None = None) -> dict:
    count = settings.rehearsal_count
    secs = settings.rehearsal_sec if seconds is None else seconds
    ctx, inputs = _session_inputs(session_id)
    qs = _generate(ctx, inputs, count, extra_rules="")
    r = _new_record(session_id, "session", mode, count, secs, session_id=session_id)
    r["questions"] = [_q_record(q) for q in qs]
    save(r)
    return r


def create_standalone(repo: str, purpose: str, mode: str, count: int, seconds: int) -> dict:
    root = Path(repo).expanduser().resolve()
    if not root.is_dir():
        raise RehearsalError(f"저장소 경로가 없습니다: {root}")
    ctx, inputs = _standalone_inputs(root, purpose)
    extra = ""
    if any(EXPERIMENT_HINT.search(f) for f in ctx.files):
        extra = ("- 이 저장소에는 실험 결과나 평가 스크립트가 있다. experiment_reasoning 유형"
                 "(예: 개선폭이 노이즈가 아니라고 어떻게 확인했나, 실패한 시도는 무엇이었나)을 1문항 포함한다. "
                 "experiment_reasoning은 위 유형 목록에 추가로 허용된다.\n")
    rid = f"r_{datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(2)}"
    qs = _generate(ctx, inputs, count, extra_rules=extra)
    r = _new_record(rid, "standalone", mode, count, seconds, repo_path=str(root), purpose=purpose)
    r["questions"] = [_q_record(q) for q in qs]
    save(r)
    return r


# ---------- 문답 ----------

def _deadline_left(r: dict, q: dict, now: datetime) -> int | None:
    if not r["seconds"] or not q["started_at"]:
        return None
    return int(r["seconds"] - (now - datetime.fromisoformat(q["started_at"])).total_seconds())


def _all_questions(r: dict) -> list[dict]:
    return r["questions"] + r["followups"]


def current(r: dict) -> dict | None:
    """시작했지만 아직 답하지 않은 문항."""
    return next((q for q in _all_questions(r) if q["started_at"] and q["answered_at"] is None), None)


def next_question(rid: str) -> dict | None:
    """다음 문항을 시작한다. 진행 중인 문항이 있으면 그것을 돌려준다(새로고침 대비)."""
    r = load(rid)
    q = current(r)
    if q is None:
        q = next((x for x in _all_questions(r) if x["started_at"] is None), None)
        if q is None:
            return None
        q["started_at"] = sessions.now_iso()
        save(r)
    return _public_question(r, q)


def answer(rid: str, qid: str, text: str, timed_out: bool) -> dict:
    r = load(rid)
    q = next((x for x in _all_questions(r) if x["id"] == qid), None)
    if q is None or not q["started_at"]:
        raise RehearsalError("시작하지 않은 문항입니다.")
    if q["answered_at"]:
        raise RehearsalError("이미 답한 문항입니다.")
    now = datetime.now().astimezone()
    left = _deadline_left(r, q, now)
    q["answer"] = text
    q["answered_at"] = now.isoformat(timespec="seconds")
    q["elapsed_sec"] = int((now - datetime.fromisoformat(q["started_at"])).total_seconds())
    q["timed_out"] = timed_out or (left is not None and left <= 0)
    q["late"] = left is not None and left < -GRACE_SEC
    save(r)
    return {"answered": qid, "remaining_questions": sum(1 for x in _all_questions(r) if x["answered_at"] is None)}


# ---------- 채점 ----------

def verdict(steps: dict) -> str:
    """6.5 규칙: 세 단계 모두 met → 충족, met ≥1이고 missed ≤1 → 부분충족, 그 외 미충족."""
    vals = [steps["weak_point"], steps["impact"], steps["plan"]]
    met, missed = vals.count("met"), vals.count("missed")
    if met == 3:
        return "충족"
    if met >= 1 and missed <= 1:
        return "부분충족"
    return "미충족"


def _grade_one(r: dict, q: dict, ctx: codectx.CodeContext) -> dict:
    if not (q["answer"] or "").strip():
        steps = {"weak_point": "missed", "impact": "missed", "plan": "missed"}
        return {"steps": steps, "verdict": verdict(steps), "misconception": None, "revisit": [],
                "comment": "답변이 비어 있습니다. 세 단계(약점 지점 → 파급 → 검증·개선 계획)로 짧게라도 적어 보세요."}
    ma = q["model_answer"]
    coords = [e for s in ("weak_point", "impact", "plan") for e in ma[s]["evidence"]]
    fmt = lambda s: f"{ma[s]['text']} (근거: {', '.join(ma[s]['evidence'])})"  # noqa: E731
    time_note = f"{r['seconds'] // 60}분 {r['seconds'] % 60}초 안에".replace(" 0초", "") if r["seconds"] else "시간 제한 없이"
    if r["mode"] == "open_book":
        time_note += " (오픈북)"
    system = prompts.load("rehearsal_grade", time_note=time_note, question=q["question"],
                          weak_point=fmt("weak_point"), impact=fmt("impact"), plan=fmt("plan"),
                          code_excerpt=ctx.excerpt(coords), answer=q["answer"])
    g = llm.parse(model=settings.model_strong, system=system, user="채점하라.", schema=GradeOutput,
                  purpose="rehearsal_grade")
    steps = {"weak_point": g.weak_point, "impact": g.impact, "plan": g.plan}
    return {"steps": steps, "verdict": verdict(steps), "misconception": g.misconception,
            "revisit": g.revisit, "comment": g.comment}


def _context_for(r: dict) -> codectx.CodeContext:
    if r["kind"] == "session":
        d = sessions.session_dir(r["session_id"])
        return codectx.build(codectx.extract_snapshot(d / "snapshot.tar.gz", d / "snapshot"))
    return codectx.build(Path(r["repo_path"]))


def scores(questions: list[dict]) -> dict:
    graded = [q for q in questions if q["grade"]]

    def qscore(q):
        return sum(STEP_SCORE[v] for v in q["grade"]["steps"].values()) / 3

    by_cat = {}
    for cat, label in CATEGORIES.items():
        qs = [q for q in graded if q["category"] == cat]
        by_cat[cat] = {"label": label, "questions": len(qs),
                       "score": round(100 * sum(map(qscore, qs)) / len(qs)) if qs else None}
    verdicts = [q["grade"]["verdict"] for q in graded]
    return {
        "total": round(100 * sum(map(qscore, graded)) / len(graded)) if graded else None,
        "by_category": by_cat,
        "verdict_counts": {v: verdicts.count(v) for v in ("충족", "부분충족", "미충족")},
    }


def grade(rid: str) -> dict:
    r = load(rid)
    todo = [q for q in _all_questions(r) if q["answered_at"] and not q["grade"]]
    if any(q["answered_at"] is None for q in _all_questions(r)):
        raise RehearsalError("아직 답하지 않은 문항이 있습니다.")
    ctx = _context_for(r)
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(lambda q: _grade_one(r, q, ctx), todo))
    for q, g in zip(todo, results):
        q["grade"] = g
    r["scores"] = scores(r["questions"])  # 인지 축 점수는 본 문항만 (재심문 제외)
    if r["followups"]:
        r["followup_scores"] = scores(r["followups"])
    r["status"] = "graded"
    r["graded_at"] = sessions.now_iso()
    save(r)
    if r["kind"] == "session":
        s = sessions.load_session(r["session_id"])
        if s.status == "submitted":
            s.status = "rehearsed"
            sessions.save_session(s)
    return r


# ---------- 재심문 ----------

def can_followup(r: dict) -> bool:
    return r["status"] == "graded" and not r["followups"] and \
        sum(q["grade"]["verdict"] == "미충족" for q in r["questions"] if q["grade"]) >= 2


def create_followup(rid: str) -> dict:
    r = load(rid)
    if not can_followup(r):
        raise RehearsalError("재심문 조건(미충족 2문항 이상, 아직 재심문 안 함)을 만족하지 않습니다.")
    missed = [q for q in r["questions"] if q["grade"]["verdict"] == "미충족"]
    n = min(2, len(missed))
    if r["kind"] == "session":
        ctx, inputs = _session_inputs(r["session_id"])
    else:
        ctx, inputs = _standalone_inputs(Path(r["repo_path"]), r.get("purpose", ""))
    focus = "\n".join(f"- {q['question']} (응시자 답: {_cap(q['answer'] or '(빈 답)', 300)})" for q in missed)
    extra = (f"- 이번은 재심문이다. 아래 미충족 문항의 주제를 다른 각도에서 다시 묻는 {n}문항을 낸다. "
             "이번에는 design_reason/failure_response 포함 규칙을 적용하지 않는다. "
             "experiment_reasoning도 허용된다.\n" + focus + "\n")
    feedback, errors = "", []
    for _ in range(MAX_ATTEMPTS):
        system = prompts.load("rehearsal_generate", count=n, file_tree=ctx.tree_text(),
                              key_files=ctx.key_files_text(), extra_rules=extra + feedback, **inputs)
        out = llm.parse(model=settings.model_strong, system=system, user="재심문 문항을 출제하라.",
                        schema=GeneratedQuestions, purpose="rehearsal_followup")
        errors = [e for e in validate(out.questions, n, ctx) if "유형 문항이 없음" not in e]
        if not errors:
            break
        feedback = "\n[이전 출제 실패 이유]\n" + "\n".join(f"- {e}" for e in errors) + "\n"
    else:
        raise RehearsalError("재심문 출제 실패: " + "; ".join(errors))
    for i, q in enumerate(out.questions, 1):
        q.id = f"F{i}"
        r["followups"].append(_q_record(q, followup=True))
    r["status"] = "in_progress"
    save(r)
    return public_view(r)


# ---------- 화면용 ----------

def _public_question(r: dict, q: dict) -> dict:
    allq = _all_questions(r)
    left = _deadline_left(r, q, datetime.now().astimezone())
    return {"id": q["id"], "index": allq.index(q) + 1, "total": len(allq), "followup": q["followup"],
            "type": q["type"], "question": q["question"], "seconds": r["seconds"],
            "remaining_sec": None if left is None else max(0, left)}


def public_view(r: dict) -> dict:
    """채점 전에는 모범 답안·채점을 빼고, 시작한 문항의 질문만 보여준다."""
    graded = r["status"] == "graded"
    out = {k: r[k] for k in ("rehearsal_id", "kind", "mode", "count", "seconds", "status", "created_at")}
    out["repo_path"] = r.get("repo_path")
    out["session_id"] = r.get("session_id")
    out["answered"] = sum(1 for q in _all_questions(r) if q["answered_at"])
    out["total"] = len(_all_questions(r))
    cur = current(r)
    out["current"] = _public_question(r, cur) if cur else None
    if graded:
        out["questions"] = r["questions"]
        out["followups"] = r["followups"]
        out["scores"] = r["scores"]
        out["followup_scores"] = r.get("followup_scores")
        out["can_followup"] = can_followup(r)
    return out


def list_standalone() -> list[dict]:
    d = settings.data_dir / "rehearsals"
    out = []
    for p in sorted(d.glob("*/rehearsal.json"), reverse=True) if d.exists() else []:
        r = json.loads(p.read_text(encoding="utf-8"))
        out.append({"rehearsal_id": r["rehearsal_id"], "repo_path": r["repo_path"], "status": r["status"],
                    "created_at": r["created_at"], "total": (r.get("scores") or {}).get("total")})
    return out
