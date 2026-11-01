"""README 장문화 · 묻힌 사실 · 설계 개념 축 (docs/flow_drills_design.md 5.3)."""
import random

import pytest

from app import generator, metrics, persona, prompts
from app.schemas import ConceptUse, Fact, Sealed

from helpers import load_fixture
from test_m1 import BURIED_QUOTE, README_OK, fact, problem


def buried(i, importance="critical", quote=BURIED_QUOTE):
    return fact(i, "자정을 넘긴 야간 수업의 출석일은 시작한 날이다.", importance=importance, type="buried", readme_quote=quote)


def base_facts():
    return [fact(1, "환불 문의에는 반드시 잔여 회차 기준 금액을 먼저 안내한다."),
            fact(2, "임산부 회원에게는 그룹 수업을 권하지 않는다."),
            buried(3)]


def use(cid, role, fact_ids=("F01", "F03"), placements=("readme", "fact")):
    return ConceptUse(id=cid, role=role, placements=list(placements), fact_ids=list(fact_ids),
                      naive_failure="n", plan="p", qa_seeds=["q"])


def choice(core=(), light=(), qa=(), difficulty="standard", buried_n=1):
    return {"difficulty": difficulty, "buried": buried_n,
            "concepts": {"core": list(core), "light": list(light), "qa_only": list(qa)}}


# ---------- 개념 카드 ----------

def test_concept_catalogue_is_consistent():
    for k, c in generator.CONCEPTS.items():
        assert c["tier"] in ("core", "light", "qa_only")
        for a in c["fit_archetypes"] or []:
            assert a in generator.ARCHETYPES, (k, a)
        for t in generator.BANNED_README_TERMS:
            assert t not in " ".join(c["policy_questions"]), (k, t)  # 담당자 업무 판단은 업무 말로
    for a in generator.ARCHETYPES:
        fits = lambda tier: [k for k, c in generator.CONCEPTS.items() if c["tier"] == tier and generator._fits(k, a)]  # noqa: E731
        assert fits("core"), a
        assert len(fits("light")) >= 2, a
        assert len(fits("qa_only")) >= 2, a


@pytest.mark.parametrize("difficulty", ["easy", "standard", "hard"])
def test_pick_concepts_follows_mix_and_exclusion_rules(difficulty):
    mix = generator.CONCEPT_MIX[difficulty]
    for a in generator.ARCHETYPES:
        for seed in range(15):
            got = generator.pick_concepts(a, difficulty, random.Random(seed))
            for tier in ("core", "light", "qa_only"):
                assert len(got[tier]) == mix[tier], (a, seed, got)
                for k in got[tier]:
                    assert generator.CONCEPTS[k]["tier"] == tier and generator._fits(k, a)
            ids = [k for v in got.values() for k in v]
            assert len(ids) == len(set(ids))
            assert sum(generator.CONCEPTS[k]["heavy"] for k in ids) <= 1
            assert sum(generator.CONCEPTS[k]["family"] == "concurrency" for k in ids) <= 1


def test_easy_has_no_core_and_hard_has_two_light():
    assert generator.CONCEPT_MIX["easy"]["core"] == 0 and generator.CONCEPT_MIX["easy"]["light"] == 1
    c = generator.pick(None, "route_scheduling", "hard", seed=4, used_domains=set())
    assert len(c["concepts"]["core"]) == 1 and len(c["concepts"]["light"]) == 2


def test_pick_avoids_recent_core_concepts():
    recent = ["capacity_waitlist", "business_date"]
    for seed in range(30):
        c = generator.pick(None, "route_scheduling", "standard", seed=seed, used_domains=set(),
                           recent_core_concepts=recent)
        assert c["concepts"]["core"][0] not in recent


def test_pick_is_reproducible_with_seed():
    a = generator.pick(None, None, "hard", seed=7, used_domains=set())
    b = generator.pick(None, None, "hard", seed=7, used_domains=set())
    assert a == b


def test_concepts_block_lists_roles_and_simplification():
    text = generator.concepts_block({"core": ["business_date"], "light": ["permissions"], "qa_only": ["audit_log"]})
    assert "id=business_date" in text and "[core]" in text and "[qa_only]" in text
    assert generator.CONCEPTS["business_date"]["simplification"] in text
    assert "빈 배열" in generator.concepts_block({"core": [], "light": [], "qa_only": []})


def test_generate_prompt_includes_concepts_and_length(monkeypatch):
    seen = {}

    def fake_parse(**kw):
        seen["system"] = kw["system"]
        return problem(facts=base_facts(), concepts=[use("business_date", "core"), use("permissions", "light"),
                                                     use("audit_log", "qa_only", fact_ids=(), placements=())])
    monkeypatch.setattr(generator.llm, "parse", fake_parse)
    c = generator.pick(None, "route_scheduling", "standard", seed=3, used_domains=set())
    c.update(buried=1, concepts={"core": ["business_date"], "light": ["permissions"], "qa_only": ["audit_log"]})
    generator.generate(c)
    assert "{{" not in seen["system"]
    assert "id=business_date" in seen["system"]
    lo, hi = generator.README_LEN["standard"]["target"]
    assert f"{lo}~{hi}자" in seen["system"]
    assert "## 업무 정책" in seen["system"] and "## 제약 사항" in seen["system"]


# ---------- 검증 ----------

def test_long_readme_with_concepts_validates():
    p = problem(facts=base_facts(), concepts=[use("business_date", "core"), use("permissions", "light", fact_ids=("F02",))])
    assert generator.validate(p, choice(core=["business_date"], light=["permissions"])) == []


def test_readme_length_depends_on_difficulty():
    n = generator.readme_length(README_OK)
    assert generator.README_LEN["easy"]["accept"][0] <= n
    hard_min = generator.README_LEN["hard"]["accept"][0]
    if n < hard_min:
        errs = generator.validate(problem(facts=base_facts()), choice(difficulty="hard"))
        assert any("README 길이" in e for e in errs)


def test_short_readme_rejected():
    errs = generator.validate(problem(readme=README_OK[:2000], facts=base_facts()))
    assert any("README 길이" in e for e in errs)


def test_new_sections_required():
    errs = generator.validate(problem(readme=README_OK.replace("## 예외 처리", "## 기타")))
    assert any("예외 처리" in e for e in errs)


def test_banned_concept_terms_rejected():
    readme = README_OK.replace("## 예외 처리", "신청 처리는 상태 머신으로 관리하며 경쟁 상태를 재현하라.\n\n## 예외 처리")
    errs = " | ".join(generator.validate(problem(readme=readme)))
    assert "상태 머신" in errs and "경쟁 상태" in errs and "재현하라" in errs


def test_buried_fact_skips_leak_check_but_needs_exact_quote():
    assert generator.validate(problem(facts=base_facts())) == []
    bad = base_facts()[:2] + [buried(3, quote="야간 수업이 자정을 넘기면 출석은 끝난 날짜로 기록합니다.")]
    errs = generator.validate(problem(facts=bad))
    assert any("F03" in e and "readme_quote" in e for e in errs)


def test_buried_facts_do_not_count_as_hidden_critical():
    facts = [fact(1, "환불 문의에는 반드시 잔여 회차 기준 금액을 먼저 안내한다."), buried(2), buried(3)]
    errs = generator.validate(problem(facts=facts))
    assert any("critical 숨은 사실" in e for e in errs)


def test_too_few_buried_facts_rejected():
    errs = generator.validate(problem(facts=base_facts()[:2]), choice(buried_n=3))
    assert any("묻힌 사실" in e for e in errs)


def test_core_concept_needs_hidden_critical_and_buried_link():
    ch = choice(core=["business_date"])
    no_buried = generator.validate(problem(facts=base_facts(), concepts=[use("business_date", "core", fact_ids=("F01",))]), ch)
    assert any("묻힌 사실" in e for e in no_buried)
    no_hidden = generator.validate(problem(facts=base_facts(), concepts=[use("business_date", "core", fact_ids=("F03",))]), ch)
    assert any("critical 숨은 사실" in e for e in no_hidden)
    one_place = generator.validate(problem(facts=base_facts(), concepts=[use("business_date", "core", placements=("fact",))]), ch)
    assert any("readme와 fact" in e for e in one_place)


def test_concept_output_must_match_choice():
    ch = choice(core=["business_date"], light=["permissions"])
    errs = " | ".join(generator.validate(problem(facts=base_facts(), concepts=[
        use("business_date", "light"), use("made_up", "light"), use("permissions", "light", fact_ids=("F99",))]), ch))
    assert "role은 core" in errs and "made_up" in errs and "F99" in errs
    missing = generator.validate(problem(facts=base_facts(), concepts=[use("business_date", "core")]), ch)
    assert any("permissions" in e for e in missing)


# ---------- 다른 단계 연결 ----------

def test_discovery_rates_exclude_buried():
    _, sealed = load_fixture()
    sealed = Sealed(**{**sealed.model_dump(), "facts": [*sealed.facts, Fact(
        id="F99", statement="s", type="buried", importance="critical", reveal_when="r", example_question="q", readme_quote="x")]})
    disc = metrics.discovery(sealed, [])
    assert metrics._rate(sealed, disc, lambda f: True)["total"] == len(sealed.facts) - 1
    assert metrics._rate(sealed, disc, lambda f: f.importance == "critical")["total"] == \
        sum(f.importance == "critical" and f.type != "buried" for f in sealed.facts)


def test_persona_gets_readme_and_buried_section():
    problem_pub, sealed = load_fixture()
    sealed = Sealed(**{**sealed.model_dump(), "facts": [*sealed.facts, Fact(
        id="F99", statement="묻힌 규칙", type="buried", importance="major", reveal_when="r", example_question="q", readme_quote="x")]})
    text = persona.render_system(problem_pub, sealed)
    section = lambda tag: text.split(f"<{tag}>")[1].split(f"</{tag}>")[0]  # noqa: E731
    assert "[F99]" in section("in_the_document") and "[F99]" not in section("things_only_you_know")
    assert problem_pub.readme_markdown.strip()[:30] in section("document_given_to_engineer")


def test_rehearsal_prompt_has_concept_seeds_slot():
    text = prompts.load("rehearsal_generate", count=6, file_tree="t", key_files="k", extra_rules="", readme="r",
                        scope_and_acceptance="s", docs="d", chat="c", agent_log_summary="a", concept_seeds="SEED")
    assert "SEED" in text and "{{" not in text


def test_old_sealed_without_concepts_still_loads():
    _, sealed = load_fixture()
    assert sealed.concepts == []
    assert all(f.readme_quote is None for f in sealed.facts)


def test_reveal_shows_buried_reflection_and_concepts(monkeypatch):
    import json
    from app import llm, reveal, sessions
    from app.config import settings
    from app.schemas import RevealMissed, Session
    problem_pub, sealed = load_fixture()
    sealed = Sealed(**{**sealed.model_dump(),
                       "facts": [*sealed.facts, Fact(id="F99", statement="묻힌 규칙", type="buried", importance="critical",
                                                     reveal_when="r", example_question="q", readme_quote="원문 인용")],
                       "concepts": [use("business_date", "core", fact_ids=("F01", "F99")).model_dump(),
                                    use("audit_log", "qa_only", fact_ids=(), placements=()).model_dump()]})
    sid, _ = sessions.new_ids()
    sessions.save_new(Session(session_id=sid, problem_id=problem_pub.problem_id, workspace_path="/tmp/ws_fixture",
                              duration_min=settings.duration_min, status="evaluated"), problem_pub, sealed)
    d = sessions.session_dir(sid)
    (d / "report.json").write_text(json.dumps({"scores": {}, "intent": {"next_actions": [], "metrics": {}}}))
    (d / "policy_checks.json").write_text(json.dumps([
        {"ref": "F99", "verdict": "미반영", "evidence": [], "note": "빠짐"},
        {"ref": "business_date", "verdict": "부분 반영", "evidence": ["app.py:3"], "note": "경계 시각 누락"}]))
    seen = {}

    def fake_parse(**kw):
        seen["system"] = kw["system"]
        return RevealMissed(items=[])
    monkeypatch.setattr(llm, "parse", fake_parse)
    rv = reveal.reveal(sid)
    assert "F99" not in seen["system"]  # 묻힌 사실은 질문 분석 대상이 아님
    f99 = next(x for x in rv["facts"] if x["id"] == "F99")
    assert f99["reflection"]["verdict"] == "미반영" and f99["readme_quote"] == "원문 인용"
    concepts = {c["id"]: c for c in rv["concepts"]}
    assert concepts["business_date"]["check"]["verdict"] == "부분 반영"
    assert concepts["business_date"]["simplification"] == generator.CONCEPTS["business_date"]["simplification"]
    assert concepts["audit_log"]["role_label"] == "Q&A 전용"
    md = rv["retro_md"]
    assert "README에 묻힌 사실 반영: 0/1" in md and "F99 묻힌 규칙 (미반영)" in md
    weak = md.split("놓친 설계 판단")[1].split("## AI 협업")[0]
    assert "- 업무 기준일과 달력 날짜: n\n    → p" in weak  # 핵심 개념: 순진한 구현 → 개선
    assert generator.CONCEPTS["audit_log"]["name"] not in weak  # Q&A 전용 개념은 회고 목록에 넣지 않음


def test_policy_targets_are_buried_and_implemented_concepts():
    from app import evaluate
    _, sealed = load_fixture()
    sealed = Sealed(**{**sealed.model_dump(),
                       "facts": [*sealed.facts, Fact(id="F99", statement="s", type="buried", importance="major",
                                                     reveal_when="r", example_question="q", readme_quote="x")],
                       "concepts": [use("business_date", "core").model_dump(), use("permissions", "light").model_dump(),
                                    use("audit_log", "qa_only").model_dump()]})
    assert evaluate.policy_targets(sealed) == ["F99", "business_date", "permissions"]
    assert "README에 명시" in evaluate.facts_text(sealed, metrics.discovery(sealed, []))
    assert generator.CONCEPTS["business_date"]["simplification"] in evaluate.concepts_text(sealed)
