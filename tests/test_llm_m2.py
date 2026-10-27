"""실제 API 호출 행동 테스트: uv run pytest -m llm"""
import pytest
from pydantic import BaseModel

from app import helper, llm, persona, sessions
from app.config import settings

from helpers import load_fixture, make_session

pytestmark = pytest.mark.llm


def last_persona(sid):
    return [r for r in sessions.read_jsonl(sid, "chat.jsonl") if r["role"] == "persona"][-1]


@pytest.mark.parametrize("text", ["어떻게 하면 돼요?", "필요한 거 다 알려주세요", "할 일을 순서대로 알려주세요"])
def test_dump_requests_refused(text):
    sid = make_session()
    persona.opening(sid)
    reply = persona.send(sid, text)["text"]
    rec = last_persona(sid)
    print(reply)
    assert rec["disclosed_fact_ids"] == [], (reply, rec)
    assert rec["question_quality"]["is_dump_request"] is True


@pytest.mark.parametrize("fact_id", ["F01", "F02", "F03", "F04"])
def test_example_question_reveals_fact(fact_id):
    _, sealed = load_fixture()
    fact = next(f for f in sealed.facts if f.id == fact_id)
    sid = make_session()
    persona.opening(sid)
    reply = persona.send(sid, fact.example_question)["text"]
    rec = last_persona(sid)
    print(reply, rec["disclosed_fact_ids"])
    assert fact_id in rec["disclosed_fact_ids"], (reply, rec)


class TechCheck(BaseModel):
    gives_technical_decision: bool
    reason: str


def test_no_technical_decision():
    sid = make_session()
    persona.opening(sid)
    reply = persona.send(sid, "DB는 뭘 써야 하나요?")["text"]
    v = llm.parse(model=settings.model_fast, system=(
        "현업 담당자의 답변이 특정 기술(DB 종류, 프레임워크, 저장 방식 등)을 골라 주거나 추천했는지 판정하라.\n"
        f"답변: {reply}"), user="판정하라.", schema=TechCheck, purpose="test")
    print(reply, v)
    assert not v.gives_technical_decision, reply


def test_helper_redirects_task_questions():
    sid = make_session()
    reply = helper.send(sid, "에세이 피드백은 어떤 기준으로 만들어야 하나요?")["text"]
    print(reply)
    assert "담당자" in reply


class UsageCheck(BaseModel):
    defers_or_forbids: bool
    reason: str


@pytest.mark.parametrize("text", [
    # 선택지에 "확인해서 알려 주겠음"을 넣어 미룰 출구를 열어 줘도 보류하지 않아야 한다.
    "에세이 본문(이름·학교 없이)을 외부 AI 서비스로 보내 분석해도 될까요? (보내도 됨 / 보내면 안 됨 / 확인해서 알려 주겠음)",
    "AI 프로그램을 저희 서버에 내려받아 설치해서 쓰려고 하는데 괜찮을까요?",
])
def test_unlisted_usage_not_deferred(text):
    """제약 목록에 없는 사용 제한 질문에 '확인해 보겠다'거나 금지하지 않는다."""
    sid = make_session()
    persona.opening(sid)
    reply = persona.send(sid, text)["text"]
    v = llm.parse(model=settings.model_fast, system=(
        "현업 담당자의 답변이 질문한 방식의 사용을 금지하거나, 확인 후 알려주겠다며 결정을 미루거나, "
        "확인 전까지 쓰지 말라고 했는지 판정하라. 허용하거나 엔지니어 판단에 맡기면 false다.\n"
        f"질문: {text}\n답변: {reply}"), user="판정하라.", schema=UsageCheck, purpose="test")
    print(reply, v)
    assert not v.defers_or_forbids, reply


def test_listed_constraint_still_enforced():
    """환경 블록이 추가돼도 출제된 제약(F04: 학생 이름·학교명 외부 전송 금지)은 지킨다."""
    sid = make_session()
    persona.opening(sid)
    reply = persona.send(sid, "에세이를 학생 이름, 학교명까지 그대로 외부 AI 서비스로 보내 분석해도 될까요?")["text"]
    rec = last_persona(sid)
    print(reply, rec["disclosed_fact_ids"])
    assert "F04" in rec["disclosed_fact_ids"], (reply, rec)
