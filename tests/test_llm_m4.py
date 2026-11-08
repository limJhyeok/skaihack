"""리허설 출제·채점 행동 테스트 (실제 API): uv run pytest -m llm -k m4"""
from pathlib import Path

import pytest

from app import codectx, rehearsal

pytestmark = pytest.mark.llm
SAMPLE = Path(__file__).parent / "fixtures" / "sample_app"
STEP_SCORE = rehearsal.STEP_SCORE


@pytest.fixture(scope="module")
def generated():
    return rehearsal.create_standalone(str(SAMPLE), "메모 요약 API", "closed_book", 5, 180)


def test_generation_meets_acceptance(generated):
    qs = generated["questions"]
    assert len(qs) == 5
    types = [q["type"] for q in qs]
    assert "design_reason" in types and "failure_response" in types
    names = rehearsal._code_names(codectx.build(SAMPLE))
    for q in qs:
        assert not rehearsal.leaked_identifiers(q["question"], names), q["question"]
        for step in ("weak_point", "impact", "plan"):
            assert q["model_answer"][step]["text"].strip()
            assert any(":" in e and e.split(":")[-1].strip().split("-")[0].isdigit() for e in q["model_answer"][step]["evidence"])


def test_grading_discriminates(generated):
    r = rehearsal.load(generated["rehearsal_id"])
    q = dict(next(x for x in r["questions"] if x["type"] == "failure_response"))
    ma = q["model_answer"]
    ctx = rehearsal._context_for(r)
    answers = {
        "wrong": "이 부분은 FastAPI가 알아서 재시도하고 예외도 자동으로 처리해 주기 때문에 별다른 위험이 없습니다.",
        "reason_only": ma["impact"]["text"],
        "full": f"약점: {ma['weak_point']['text']} 파급: {ma['impact']['text']} 계획: {ma['plan']['text']}",
    }
    grades = {}
    for k, a in answers.items():
        q["answer"] = a
        grades[k] = rehearsal._grade_one(r, q, ctx)
        print(k, grades[k]["verdict"], grades[k]["steps"])
    score = {k: sum(STEP_SCORE[v] for v in g["steps"].values()) for k, g in grades.items()}
    assert grades["wrong"]["verdict"] == "미충족"
    assert grades["full"]["verdict"] == "충족"
    assert grades["reason_only"]["steps"]["plan"] != "met"
    assert score["wrong"] < score["reason_only"] < score["full"]
