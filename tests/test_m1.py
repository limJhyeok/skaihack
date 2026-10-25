import dataclasses
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app import config, generator, prompts, seal, workspace
from app.schemas import DataFile, Fact, GeneratedProblem, PersonaPrivate, PersonaPublic, ProblemPublic

README_OK = """# 테스트 과제

## 상황
""" + "동네 필라테스 스튜디오는 회원 상담을 강사들이 수업 사이에 직접 처리합니다. " * 6 + """

## 현업의 요청
> "상담 답변을 빨리 쓰고 싶어요. 제 기준도 담고 싶고요."

## 현업 파트너
김하늘 실장은 개발자가 아닙니다. 범위를 정해 합의하는 것도 과제의 일부입니다.

## 과제
상담 답변 초안을 돕는 웹서비스를 만드세요. 구현 방식은 응시자 판단에 맡깁니다.

## 실행 환경
OpenAI API는 OPENAI_API_KEY, OPENAI_BASE_URL 환경변수로 제공됩니다. 코드에 쓰거나 커밋하지 마세요.
"""


def fact(i, statement, importance="critical", **kw):
    base = dict(id=f"F{i:02d}", statement=statement, type="ask_to_reveal", importance=importance,
                reveal_when="환불 규정을 물을 때", example_question="환불은 어떻게 처리하세요?")
    return Fact(**{**base, **kw})


def problem(readme=README_OK, facts=None, data_files=()):
    return GeneratedProblem(
        title="t", readme_markdown=readme, data_files=list(data_files),
        persona_public=PersonaPublic(name="김하늘", role="실장", intro="안녕하세요"),
        persona_private=PersonaPrivate(speaking_style="짧게", answerable_topics=["상담"], background="b"),
        facts=facts or [fact(1, "환불 문의에는 반드시 잔여 회차 기준 금액을 먼저 안내한다."),
                        fact(2, "임산부 회원에게는 그룹 수업을 권하지 않는다.")],
        priority_confusion=[], good_solution_criteria=[], overengineering_traps=[], data_file_notes="",
    )


# ---------- 암호화 ----------

def test_seal_roundtrip():
    key = Fernet.generate_key()
    token = seal.seal("숨은 사실 F01", key)
    assert "숨은".encode() not in token
    assert seal.unseal(token, key) == "숨은 사실 F01"


def test_seal_wrong_key_fails():
    token = seal.seal("x", Fernet.generate_key())
    with pytest.raises(InvalidToken):
        seal.unseal(token, Fernet.generate_key())


# ---------- 경로 겹침 ----------

def test_overlapping_workspace_rejected(tmp_path):
    with pytest.raises(SystemExit):
        config.check_paths(tmp_path / "app" / "ws", tmp_path / "app")
    with pytest.raises(SystemExit):
        config.check_paths(tmp_path, tmp_path / "app")
    config.check_paths(tmp_path / "ws", tmp_path / "app")  # 형제 디렉터리는 허용


# ---------- 프롬프트 치환 ----------

def test_render_vars_and_missing():
    assert prompts.render("a {{x}} b {{ y }}", x=1, y="둘") == "a 1 b 둘"
    with pytest.raises(KeyError):
        prompts.render("{{nope}}")


def test_generate_prompt_has_no_unfilled_vars():
    text = prompts.load("generate_problem", domain="d", archetype="a", archetype_description="ad",
                        difficulty="easy", fact_count=6, conflict_count=0, constraint_count=1,
                        data_file_count=0, trap_count=2, example_readme="ex", retry_feedback="")
    assert "{{" not in text


# ---------- README 노출 검사 / 출력 검증 ----------

def test_validate_ok():
    assert generator.validate(problem()) == []


def test_leak_detected_in_readme():
    leaked = README_OK.replace("## 과제", "환불 문의에는 반드시 잔여 회차 기준 금액을 먼저 안내한다.\n\n## 과제")
    errs = generator.validate(problem(readme=leaked))
    assert any("F01" in e and "노출" in e for e in errs)


def test_leak_detected_in_data_file():
    df = DataFile(path="data/faq.txt", content="메모: 임산부 회원에게는 그룹 수업을 권하지 않는다")
    errs = generator.validate(problem(data_files=[df]))
    assert any("F02" in e for e in errs)


def test_unrelated_statement_not_flagged():
    assert not generator.leaks("주말 오전 수업은 정원이 6명으로 제한된다.", README_OK)


def test_validate_catches_structure_problems():
    bad = problem(readme="# 짧음\n## 상황\n짧다", facts=[fact(1, "가", importance="major", example_question=" ")])
    errs = " | ".join(generator.validate(bad))
    for needle in ["현업의 요청", "실행 환경", "길이", "critical", "example_question"]:
        assert needle in errs


def test_validate_rejects_bad_data_path():
    errs = generator.validate(problem(data_files=[DataFile(path="../x.csv", content="a")]))
    assert any("경로" in e for e in errs)


def test_pick_avoids_used_domains_and_respects_ranges():
    used = set(generator.DOMAINS[:-1])
    c = generator.pick(None, None, "hard", seed=1, used_domains=used)
    assert c["domain"] == generator.DOMAINS[-1]
    assert 13 <= c["facts"] <= 16 and c["constraint"] == 3


# ---------- 작업공간 ----------

def test_workspace_create(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "settings", dataclasses.replace(config.settings, workspace_root=tmp_path))
    p = ProblemPublic(problem_id="p", title="t", domain="d", archetype="a", difficulty="easy",
                      readme_markdown=README_OK, data_files=[DataFile(path="data/a.csv", content="x,y\n")],
                      persona_public=PersonaPublic(name="n", role="r", intro="i"))
    ws = workspace.create("s_test", p)
    for name in ["README.md", "data/a.csv", ".env.example", ".gitignore"]:
        assert (ws / name).exists(), name
    assert ".env" in (ws / ".gitignore").read_text().splitlines()
    log = subprocess.run(["git", "log", "--format=%s"], cwd=ws, capture_output=True, text=True).stdout
    assert log.strip() == "initial: problem files"
    # 앱 경로 힌트가 작업공간에 없어야 한다
    app_root = str(config.APP_ROOT)
    for f in ws.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            assert app_root not in f.read_text(errors="ignore")
