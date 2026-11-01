"""동작 추적 카드 드릴. LLM은 대체하고, probe는 작은 저장소에서 실제로 실행한다 (uv 필요)."""
import shutil
import tarfile

import pytest
from fastapi.testclient import TestClient

from app import cards, llm, probe, sessions
from app.config import settings
from app.main import app
from app.schemas import CardRepair, CardSlot, GeneratedCard, GeneratedDeck

from helpers import make_session

client = TestClient(app)

CALC = '''import os


def parse(text):
    """빈 문자열은 오류가 아니라 빈 결과로 처리한다 (직관과 다른 동작)."""
    if not text:
        return {"items": [], "error": None}
    return {"items": text.split(","), "error": None}


def key_seen():
    return os.environ.get("OPENAI_API_KEY", "")
'''


def _card(cid, claimed_count, claimed_err, probe_code=None):
    probe_code = probe_code or '''
from calc import parse
def test_p():
    r = parse("")
    observe("s1", len(r["items"]))
    observe("s2", "오류" if r["error"] else "정상 빈 결과")
'''
    return GeneratedCard(
        id=cid, context="사용자가 쉼표로 구분한 목록을 보내면 항목 목록으로 나눠 돌려준다.", scenario="사용자가 빈 문자열을 보낸다.", evidence=["calc.py:4-7"], explanation="빈 입력은 빈 목록으로 반환된다.",
        defect_tags=["EMPTY_AS_SUCCESS", "NOPE"], probe=probe_code,
        slots=[CardSlot(id="s1", prompt="항목 개수는?", kind="value", options=[], claimed=claimed_count),
               CardSlot(id="s2", prompt="결과는?", kind="choice", options=["오류", "정상 빈 결과"], claimed=claimed_err)])


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "calc.py").write_text(CALC, encoding="utf-8")
    (r / "README.md").write_text("# calc\n", encoding="utf-8")
    return r


def test_norm_same():
    assert cards.same("200", 200) and cards.same(" 2.0 ", 2) and cards.same(True, "TRUE")
    assert cards.same("정상 빈 결과", "정상빈결과") and not cards.same("200", "201")


def test_structure_errors():
    c = _card("C1", "0", "정상 빈 결과")
    assert cards.structure_errors(c) == []
    bad = _card("C2", "0", "없는 선택지", probe_code="from calc import parse\ndef test_p():\n    observe('s1', 1)\n")
    errs = " ".join(cards.structure_errors(bad))
    assert "선택지에 없음" in errs and "s2: probe가 observe하지 않음" in errs


def test_supported_excludes_node_repo(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "w.py").write_text("")
    assert not probe.supported(tmp_path)
    (tmp_path / "requirements.txt").write_text("")
    assert probe.supported(tmp_path)  # Python 의존성을 선언했으면 지원


def test_text_errors_block_internal_codes_and_harness_words():
    c = _card("C1", "0", "정상 빈 결과")
    assert cards.text_errors(c, {"parse"}) == []
    bad = c.model_copy(update={"scenario": "B1 근거가 #12-1 기준과 다르고 parse가 가짜로 바뀐다.", "context": ""})
    errs = " ".join(cards.text_errors(bad, {"parse"}))
    assert "B1" in errs and "#12-1" in errs and "가짜로" in errs and "parse" in errs and "context가 비어" in errs


def test_probe_runs_isolated_without_real_key(repo):
    code = "from calc import key_seen\ndef test_p():\n    observe('k', key_seen())\n    open('written.txt', 'w').write('x')\n"
    r = probe.run_one(repo, "k", code)
    assert r["passed"], r["output"]
    assert r["observed"]["k"].startswith("sk-drill-probe")
    assert not (repo / "written.txt").exists() and not (repo / probe.PROBE_DIR).exists()  # 원본 불변


def test_verify_uses_observed_values(repo, monkeypatch):
    from app import codectx
    ctx = codectx.build(repo)
    right = _card("C1", "0", "정상 빈 결과")
    wrong = _card("C2", "1", "오류")  # 출제자 예상이 틀림 → 검수에서 관찰값 채택
    broken = _card("C3", "0", "오류", probe_code="from calc import nope\ndef test_p():\n    observe('s1', 0)\n    observe('s2', '오류')\n")
    unobserved = _card("C4", "0", "오류", probe_code="from calc import parse\ndef test_p():\n    observe('s1', 0)\n")

    def fake_repair(**kw):
        if "nope" in kw["system"]:
            return CardRepair(action="drop", probe=None, explanation=None, reason="import 실패")
        return CardRepair(action="accept_observed", probe=None, explanation="실제로는 빈 목록이 반환된다.", reason="예상 오류")
    monkeypatch.setattr(llm, "parse", fake_repair)
    kept, dropped = cards.verify([right, wrong, broken, unobserved], repo, ctx)
    by_id = {c["id"]: c for c in kept}
    assert set(by_id) == {"C1", "C2"}
    assert by_id["C1"]["surprise"] is False
    assert by_id["C2"]["surprise"] is True and by_id["C2"]["explanation"].startswith("실제로는")
    assert [s["answer"] for s in by_id["C2"]["slots"]] == ["0", "정상 빈 결과"]  # 정답은 관찰값
    assert by_id["C2"]["defect_tags"] == ["EMPTY_AS_SUCCESS"]
    assert {d["id"] for d in dropped} == {"C3", "C4"}


def test_deck_and_run_flow_via_api(repo, monkeypatch):
    shutil.rmtree(settings.data_dir / "drills", ignore_errors=True)
    sid = make_session("running")
    with tarfile.open(sessions.session_dir(sid) / "snapshot.tar.gz", "w:gz") as tar:
        tar.add(repo, arcname=".")
    deck = GeneratedDeck(cards=[_card("C1", "0", "정상 빈 결과"), _card("C2", "0", "정상 빈 결과"),
                                _card("C3", "0", "정상 빈 결과")])
    monkeypatch.setattr(llm, "parse", lambda **kw: deck)
    src = next(s for s in client.get("/api/drills/sources").json()["sources"] if s["session_id"] == sid)
    assert src["runnable"] and src["cards"] == 0
    r = client.post("/api/drills/decks", json={"session_id": sid, "count": 3})
    assert r.status_code == 200, r.text
    assert r.json()["generation"]["verified"] == 3

    run = client.post("/api/drills/runs", json={"mode": "open", "count": 2, "session_id": sid}).json()
    rid = run["run_id"]
    assert client.post(f"/api/drills/runs/{rid}/cards/2/start").status_code == 409  # 순서 강제
    q = client.post(f"/api/drills/runs/{rid}/cards/1/start").json()
    assert "answer" not in str(q["slots"]) and q["code"][0]["file"] == "calc.py"  # 코드 보고: 근거 파일 전체
    assert client.get(f"/api/drills/runs/{rid}/cards/1/result").status_code == 409  # 답 전 공개 불가
    res = client.post(f"/api/drills/runs/{rid}/cards/1/answer", json={"answers": {"s1": "0", "s2": "오류"}}).json()
    assert [s["correct"] for s in res["slots"]] == [True, False] and res["all_correct"] is False
    assert any(line["hit"] for line in res["snippets"][0]["lines"])
    client.post(f"/api/drills/runs/{rid}/cards/2/start")
    res = client.post(f"/api/drills/runs/{rid}/cards/2/answer",
                      json={"answers": {"s1": " 0 ", "s2": "정상 빈 결과"}}).json()
    assert res["all_correct"] and res["run_done"]
    v = client.get(f"/api/drills/runs/{rid}").json()
    assert v["status"] == "done" and v["summary"]["card_rate"] == 50 and v["summary"]["slot_rate"] == 75
    assert v["summary"]["missed_tags"] == {"EMPTY_AS_SUCCESS": 1}

    # 다음 드릴: 안 푼 카드 → 마지막에 틀린 카드 순
    run2 = cards.create_run("closed", 3, sid)
    keys = [it["card_key"] for it in run2["items"]]
    first_run = cards.load_run(rid)
    wrong_key = first_run["items"][0]["card_key"]
    unseen = {c["key"] for d in cards._all("decks") for c in d["cards"]} - {it["card_key"] for it in first_run["items"]}
    assert set(keys[:1]) == unseen and keys[1] == wrong_key
    assert client.get("/api/drills/runs/x_..").status_code == 404
