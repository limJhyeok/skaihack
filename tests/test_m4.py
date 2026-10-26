import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import codectx, llm, rehearsal
from app.main import app
from app.schemas import AnswerStep, GeneratedQuestions, GradeOutput, ModelAnswer, RehearsalQuestion

SAMPLE = Path(__file__).parent / "fixtures" / "sample_app"


def mk_q(i, type_, question, cat="behavior_risk"):
    step = lambda t: AnswerStep(text=t, evidence=["main.py:25"])  # noqa: E731
    return RehearsalQuestion(id=f"Q{i}", type=type_, category=cat, question=question,
                             model_answer=ModelAnswer(weak_point=step("summarize 예외 처리 없음"),
                                                      impact=step("500 에러"), plan=step("타임아웃 테스트")))


GOOD = [mk_q(1, "design_reason", "create_memo에서 summarize를 저장 전에 호출한 이유는?", "decision_rationale"),
        mk_q(2, "failure_response", "summarize 호출이 30초 걸리면 /memos는 어떻게 되나요?"),
        mk_q(3, "risk_prediction", "search 엔드포인트에서 가장 먼저 깨질 곳은?"),
        mk_q(4, "data_flow", "POST /memos 요청이 main.py에서 처리되는 순서는?", "structure"),
        mk_q(5, "requirement_mapping", "get_db는 언제 테이블을 만드나요?", "structure")]


# ---------- 규칙 ----------

@pytest.mark.parametrize("steps,expected", [
    (("met", "met", "met"), "충족"),
    (("met", "partial", "missed"), "부분충족"),
    (("met", "missed", "missed"), "미충족"),
    (("partial", "partial", "partial"), "미충족"),
    (("missed", "met", "partial"), "부분충족"),
])
def test_verdict_rule(steps, expected):
    assert rehearsal.verdict(dict(zip(("weak_point", "impact", "plan"), steps))) == expected


def test_scores_by_category():
    qs = [{"category": "structure", "grade": {"steps": {"a": "met", "b": "met", "c": "met"}, "verdict": "충족"}},
          {"category": "structure", "grade": {"steps": {"a": "missed", "b": "missed", "c": "missed"}, "verdict": "미충족"}},
          {"category": "behavior_risk", "grade": {"steps": {"a": "met", "b": "partial", "c": "missed"}, "verdict": "부분충족"}}]
    s = rehearsal.scores(qs)
    assert s["by_category"]["structure"]["score"] == 50
    assert s["by_category"]["behavior_risk"]["score"] == 50
    assert s["by_category"]["decision_rationale"]["score"] is None
    assert s["total"] == 50 and s["verdict_counts"] == {"충족": 1, "부분충족": 1, "미충족": 1}


def test_validate_questions():
    ctx = codectx.build(SAMPLE)
    assert {"summarize", "create_memo", "main.py", "/memos"} <= ctx.identifiers()
    assert rehearsal.validate(GOOD, 5, ctx) == []
    bad = [mk_q(1, "risk_prediction", "일반적으로 API 서버에서 가장 먼저 깨지는 곳은?")]
    bad[0].model_answer.plan.evidence = ["없음"]
    errs = " | ".join(rehearsal.validate(bad, 5, ctx))
    for needle in ["문항 수", "design_reason", "failure_response", "실제 파일명", "plan"]:
        assert needle in errs


def test_codectx_line_numbers_and_excerpt():
    ctx = codectx.build(SAMPLE)
    assert "main.py" in ctx.key_files and "  25| " in ctx.key_files_text()
    ex = ctx.excerpt(["main.py:25"], radius=2)
    assert "main.py:23-27" in ex and "def summarize" in ex


def test_codectx_inside_ignored_dir_of_parent_repo(tmp_path):
    # 스냅샷은 .git 없이 상위 저장소의 무시 경로(data/) 안에 풀린다 → git ls-files가 빈 목록을 준다
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("data/\n")
    snap = tmp_path / "data" / "snapshot"
    snap.mkdir(parents=True)
    (snap / "main.py").write_text((SAMPLE / "main.py").read_text())
    ctx = codectx.build(snap)
    assert ctx.files == ["main.py"] and "summarize" in ctx.identifiers()


# ---------- API 흐름 (LLM 모의) ----------

@pytest.fixture
def fake_llm(monkeypatch):
    def parse(*, schema, **kw):
        if schema is GeneratedQuestions:
            return GeneratedQuestions(questions=GOOD)
        return GradeOutput(weak_point="met", impact="partial", plan="missed", misconception=None,
                           revisit=["main.py:25"], comment="계획을 보강하세요.")
    monkeypatch.setattr(llm, "parse", parse)


def test_standalone_flow_hides_model_answers_until_graded(fake_llm):
    c = TestClient(app)
    r = c.post("/api/rehearsals", json={"repo_path": str(SAMPLE), "count": 5, "seconds": 180}).json()
    rid = r["rehearsal_id"]
    assert "questions" not in r and "model_answer" not in str(r)
    seen = []
    for i in range(5):
        q = c.post(f"/api/rehearsals/{rid}/next").json()["question"]
        # 새로고침해도 같은 문항, 다음 문항 질문은 미리 보이지 않음
        assert c.post(f"/api/rehearsals/{rid}/next").json()["question"]["id"] == q["id"]
        view = c.get(f"/api/rehearsals/{rid}").text
        assert "model_answer" not in view and "summarize 예외 처리 없음" not in view
        assert all(g.question not in view for g in GOOD[i + 1:])
        assert 170 <= q["remaining_sec"] <= 180
        seen.append(q["id"])
        answer = "" if i == 0 else "summarize에 타임아웃이 없어 요청이 막힘"
        assert c.post(f"/api/rehearsals/{rid}/questions/{q['id']}/answer", json={"answer": answer}).status_code == 200
    assert seen == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert c.post(f"/api/rehearsals/{rid}/next").json()["question"] is None
    g = c.post(f"/api/rehearsals/{rid}/grade").json()
    assert g["status"] == "graded"
    assert g["questions"][0]["grade"]["verdict"] == "미충족"  # 빈 답은 LLM 없이 미충족
    assert g["questions"][1]["grade"]["verdict"] == "부분충족"
    assert g["questions"][1]["model_answer"]["weak_point"]["evidence"] == ["main.py:25"]
    assert g["scores"]["total"] is not None


def test_cannot_answer_twice_or_unstarted(fake_llm):
    c = TestClient(app)
    rid = c.post("/api/rehearsals", json={"repo_path": str(SAMPLE)}).json()["rehearsal_id"]
    assert c.post(f"/api/rehearsals/{rid}/questions/Q2/answer", json={"answer": "x"}).status_code == 409
    c.post(f"/api/rehearsals/{rid}/next")
    c.post(f"/api/rehearsals/{rid}/questions/Q1/answer", json={"answer": "x"})
    assert c.post(f"/api/rehearsals/{rid}/questions/Q1/answer", json={"answer": "y"}).status_code == 409
    assert c.post(f"/api/rehearsals/{rid}/grade").status_code == 409  # 아직 안 푼 문항


def test_followup_after_two_misses(fake_llm, monkeypatch):
    c = TestClient(app)
    rid = c.post("/api/rehearsals", json={"repo_path": str(SAMPLE)}).json()["rehearsal_id"]
    for _ in range(5):
        q = c.post(f"/api/rehearsals/{rid}/next").json()["question"]
        c.post(f"/api/rehearsals/{rid}/questions/{q['id']}/answer", json={"answer": ""})
    g = c.post(f"/api/rehearsals/{rid}/grade").json()
    assert g["can_followup"] is True
    monkeypatch.setattr(llm, "parse", lambda *, schema, **kw: GeneratedQuestions(questions=GOOD[2:4]))
    f = c.post(f"/api/rehearsals/{rid}/followup").json()
    assert f["total"] == 7 and f["status"] == "in_progress"
    q = c.post(f"/api/rehearsals/{rid}/next").json()["question"]
    assert q["id"] == "F1" and q["followup"] is True


def test_open_book_no_time_limit(fake_llm):
    c = TestClient(app)
    rid = c.post("/api/rehearsals", json={"repo_path": str(SAMPLE), "mode": "open_book", "no_time_limit": True}).json()["rehearsal_id"]
    q = c.post(f"/api/rehearsals/{rid}/next").json()["question"]
    assert q["seconds"] == 0 and q["remaining_sec"] is None


def test_session_rehearsal_requires_submission():
    from helpers import make_session
    sid = make_session()
    assert TestClient(app).post(f"/api/sessions/{sid}/rehearsal", json={}).status_code == 409


# ---------- CLI 입력 ----------

def _reader_from(data: bytes):
    from mockdrill.__main__ import LineReader
    r, w = os.pipe()
    os.write(w, data)
    return LineReader(r), w


def test_cli_read_answer_multiline_and_terminator():
    from mockdrill.__main__ import read_answer
    reader, w = _reader_from("첫 줄\n둘째 줄\n.\n다음 답\n.\n".encode())
    assert read_answer(reader, 5) == ("첫 줄\n둘째 줄", False)
    assert read_answer(reader, 5) == ("다음 답", False)
    os.close(w)


def test_cli_read_answer_timeout_keeps_typed_lines():
    from mockdrill.__main__ import read_answer
    reader, w = _reader_from("입력한 줄\n".encode())  # 종료 표시 없이 시간 초과
    text, timed_out = read_answer(reader, 1)
    assert timed_out and text == "입력한 줄"
    os.close(w)
