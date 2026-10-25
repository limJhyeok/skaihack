import json

from fastapi.testclient import TestClient

from app import llm, persona, sessions
from app.main import app
from app.schemas import DisclosureJudgment

from helpers import load_fixture, make_session


def test_render_system_places_facts_by_type():
    problem, sealed = load_fixture()
    text = persona.render_system(problem, sealed)
    assert "{{" not in text

    def section(tag):
        return text.split(f"<{tag}>")[1].split(f"</{tag}>")[0]

    assert "[F01]" in section("things_only_you_know") and "[F05]" in section("things_only_you_know")
    assert "[F03]" in section("conflicts")
    assert "[F04]" in section("constraints") and "[F04]" not in section("things_only_you_know")
    assert "[F06]" in section("confirm_only")
    assert "박소연" in section("role")
    assert persona.render_system(problem, sealed) == text  # 결정적


def test_no_sealed_info_in_any_api_response(monkeypatch):
    """누출 테스트: 응시 중 모든 엔드포인트 응답에 봉인 사실 문장·조건이 없어야 한다."""
    monkeypatch.setattr(llm, "chat", lambda **kw: "네, 구체적으로 물어봐 주세요.")
    monkeypatch.setattr(llm, "parse", lambda **kw: DisclosureJudgment(
        disclosed_fact_ids=["F01"], partial_fact_ids=[], question_specificity=3,
        is_dump_request=False, is_confirmation=False, is_scope_proposal=False, scope_agreed=False))
    _, sealed = load_fixture()
    sid = make_session(status="created")
    c = TestClient(app)
    bodies = [
        c.get("/api/options").text,
        c.get("/api/sessions").text,
        c.get(f"/api/sessions/{sid}").text,
        c.post(f"/api/sessions/{sid}/start", json={"mode": "practice"}).text,
        c.post(f"/api/sessions/{sid}/chat", json={"text": "첨삭 기준이 궁금해요"}).text,
        c.get(f"/api/sessions/{sid}/chat").text,
        c.post(f"/api/sessions/{sid}/helper", json={"text": "제출은 어떻게 해요?"}).text,
        c.get(f"/api/sessions/{sid}/helper").text,
    ]
    static = [c.get("/").text, c.get("/app.js").text]
    api_blob = "\n".join(bodies)
    blob = "\n".join(bodies + static)
    for f in sealed.facts:
        for s in (f.statement, f.reveal_when, f.example_question):
            assert s not in blob, f"{f.id} 누출"
    for s in sealed.overengineering_traps + sealed.priority_confusion:
        assert s not in blob
    # 판정 필드는 API 응답에 없어야 한다 (정적 JS에는 리포트 렌더링용 필드명이 있으므로 제외)
    assert "disclosed" not in api_blob and "question_quality" not in api_blob and "specificity" not in api_blob
    # 판정은 서버 기록에는 남는다
    log = sessions.read_jsonl(sid, "chat.jsonl")
    assert log[0]["role"] == "persona"  # 시작 시 첫 인사
    assert log[-1]["disclosed_fact_ids"] == ["F01"]


def test_chat_requires_running():
    sid = make_session(status="created")
    r = TestClient(app).post(f"/api/sessions/{sid}/chat", json={"text": "안녕하세요"})
    assert r.status_code == 409


def test_failed_reply_keeps_message_and_resend_does_not_duplicate(monkeypatch):
    sid = make_session()

    def boom(**kw):
        raise llm.LLMError("timeout")

    monkeypatch.setattr(llm, "chat", boom)
    c = TestClient(app)
    assert c.post(f"/api/sessions/{sid}/chat", json={"text": "분량 기준이 궁금해요"}).status_code == 502
    assert [r["role"] for r in sessions.read_jsonl(sid, "chat.jsonl")] == ["user"]

    monkeypatch.setattr(llm, "chat", lambda **kw: "150~200단어예요.")
    monkeypatch.setattr(llm, "parse", lambda **kw: (_ for _ in ()).throw(llm.LLMError("judge down")))
    r = c.post(f"/api/sessions/{sid}/chat", json={"text": "분량 기준이 궁금해요"})
    assert r.status_code == 200
    log = sessions.read_jsonl(sid, "chat.jsonl")
    assert [x["role"] for x in log] == ["user", "persona"]  # 재전송이 중복 저장되지 않음
    assert "judge_error" in log[-1]  # 판정 실패는 대화를 막지 않음
