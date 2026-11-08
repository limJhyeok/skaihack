"""C(AI 변경 검토)·D(변경 대응) 드릴. LLM은 대체하고, 정답 구현·결함·변경 버전은 하네스로 실제 실행한다."""
import dataclasses
import random
import shutil

import pytest
from fastapi.testclient import TestClient

from app import change, llm, main, policy, review
from app.config import settings
from app.main import app
from app.schemas import PolicyBundleOut, PolicyChangesOut

from tests.test_policy import REF, bundle_dict

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(policy, "DOC_LEN", {"target": (100, 200), "accept": (50, 5000)})
    monkeypatch.setattr(policy, "POLICIES", (4, 12))
    monkeypatch.setattr(policy, "FUNCTIONS", (2, 5))
    monkeypatch.setattr(policy, "DEFECTS", (3, 7))
    for kind in ("bundles", "sheets", "reads", "reviews", "responses"):
        shutil.rmtree(settings.data_dir / "drills" / kind, ignore_errors=True)


def _bundle(monkeypatch) -> dict:
    monkeypatch.setattr(llm, "parse", lambda **kw: PolicyBundleOut(**bundle_dict()))
    return policy.generate(seed=2)


# ---------- 코드 조각 ----------

def test_chunk_helpers():
    assert [(c["name"], c["kind"]) for c in policy.chunks(REF)] == [("work_date", "func"), ("seat_status", "func")]
    var = REF.replace("start.hour < 6", "start.hour < 5")
    hits = policy.changed_lines(REF, var)
    assert len(hits) == 1 and "start.hour < 5" in var.splitlines()[hits[0] - 1]
    assert policy.touched_chunks(var, hits) == ["work_date"]
    ins = REF.replace("    return d\n", "    d = d\n    return d\n")
    assert policy.touched_chunks(ins, policy.changed_lines(REF, ins)) == ["work_date"]
    b = bundle_dict()
    assert policy.related_policies(b, {"work_date"}) == ["P1", "P2"]
    assert policy.related_policies(b, {"seat_status"}) == ["P3", "P4", "P5"]
    code = "RATE = {'a': 1}\n\n\ndef f(x):\n    return g(x)\n\n\ndef g(x):\n    return RATE[x]\n"
    assert policy.reach(code)["f"] == {"f", "g", "RATE"}


# ---------- C. AI 변경 검토 ----------

def test_build_cards_rules(monkeypatch):
    b = _bundle(monkeypatch)
    for seed in range(8):
        deck = review.build_cards(b, random.Random(seed))
        touched = [n for c in deck for n in c["touched"]]
        assert len(touched) == len(set(touched))            # 같은 정의는 한 번만
        assert any(c["kind"] == "defect" for c in deck)
    monkeypatch.setattr(review, "P_CORRECT", 0.0)
    deck = review.build_cards(b, random.Random(0))
    assert sorted(c["kind"] for c in deck) == ["defect", "defect"]
    examples = {e["eid"]: e for e in policy.policy_examples(b)}
    d1 = review._defect_card(b, review._defect_unit(b, b["defects"][0]), examples)
    assert d1["policies"] == ["P1", "P2"] and d1["accepted"] == ["P1"]
    line = next(l for s in d1["code"] for l in s["lines"] if l["n"] == d1["hits"][0])
    assert "end.date()" in line["text"]
    assert d1["evidence"][0]["eid"] == "P1.1" and d1["evidence"][0]["actual"] == "2026-05-14"
    monkeypatch.setattr(review, "P_CORRECT", 1.0)
    monkeypatch.setattr(review, "P_EXTRA_CORRECT", 1.0)
    deck = review.build_cards(b, random.Random(0))
    assert [c["kind"] for c in deck].count("defect") == 1   # 전부 정상이면 하나는 결함으로 바꾼다


def test_review_api_flow(monkeypatch):
    b = _bundle(monkeypatch)
    monkeypatch.setattr(review, "P_CORRECT", 0.0)
    v = client.post("/api/review/decks", json={"bundle_id": b["bundle_id"], "seed": 1}).json()
    rid = v["review_id"]
    text = client.get(f"/api/review/decks/{rid}").text
    for secret in ("defect_id", "accepted", "퇴근 날짜로", "\"hit\":true", "reference_code"):
        assert secret not in text
    stored = review.load(rid)
    first, second = stored["cards"]
    assert v["question"]["n"] == 1 and len(v["question"]["policies"]) == len(first["policies"])
    # 1번: 정책·줄까지 정답
    r = client.post(f"/api/review/decks/{rid}/cards/1/answer",
                    json={"verdict": "violation", "policy_id": first["accepted"][0], "line": first["hits"][0]}).json()
    assert r["all_correct"] and r["policy"]["id"] == first["accepted"][0] and r["evidence"]
    assert any(l["hit"] for s in r["code"] for l in s["lines"]) and r["ref_code"]
    assert client.post(f"/api/review/decks/{rid}/cards/1/answer", json={"verdict": "ok"}).status_code == 409
    bad = client.post(f"/api/review/decks/{rid}/cards/2/answer", json={"verdict": "violation", "policy_id": "P99"})
    assert bad.status_code == 409
    # 2번: 결함을 받아들임 → 놓침
    r = client.post(f"/api/review/decks/{rid}/cards/2/answer", json={"verdict": "ok"}).json()
    assert not r["verdict_ok"] and not r["all_correct"] and r["done"]
    v = client.get(f"/api/review/decks/{rid}").json()
    assert v["question"] is None
    s = v["summary"]
    assert (s["all_correct"], s["defects"], s["caught"], s["catch_rate"], s["false_alarm"]) == (1, 2, 1, 50, 0)
    assert client.get(f"/api/review/decks/{rid}/cards/2/result").json()["n"] == 2
    ov = client.get("/api/review").json()
    assert ov["bundles"][0]["defects_seen"] == 2 and ov["reviews"][0]["done"]
    # 다른 드릴 목록에 "이미 봄"으로 표시
    assert client.get("/api/reading").json()["bundles"][0]["seen"]
    assert client.get("/api/criteria").json()["bundles"][0]["seen"]


def test_review_correct_card_grading():
    c = {"kind": "correct", "answer": {"verdict": "violation", "policy_id": "P1", "line": 3}}
    assert review.grade_card(c) == {"verdict_ok": False, "policy_ok": None, "line_ok": None, "all_correct": False}
    c = {"kind": "defect", "accepted": ["P1", "P2"], "hits": [4, 5],
         "answer": {"verdict": "violation", "policy_id": "P2", "line": 5}}
    assert review.grade_card(c)["all_correct"]
    c["answer"]["line"] = None
    assert review.grade_card(c) == {"verdict_ok": True, "policy_ok": True, "line_ok": False, "all_correct": False}


# ---------- D. 변경 대응 ----------

def _ch(cid, edits, pols=("P5",), notice="다음 달부터 대기 안내 방식을 바꿉니다."):
    return {"id": cid, "title": f"변경 {cid}", "notice": notice,
            "changed_policies": [{"policy_id": p, "statement": "새 규칙"} for p in pols],
            "edits": [{"find": f, "replace": r} for f, r in edits]}


GOOD = [
    _ch("C1", [('return f"대기 {i - capacity + 1}번"', 'return "대기"')]),
    _ch("C2", [("    if start.hour < 6:", "    if start.hour < 6 or start.hour >= 22:")], pols=("P1",),
        notice="야간 22시 이후 출근도 전날 근무로 봅니다."),
]
BAD = [
    _ch("C3", [("start.hour < 6", "start.hour < 7")], pols=("P2",), notice="새벽 기준을 7시로 늦춥니다."),   # 바뀌는 예시 없음
    _ch("C4", [("active = [a for a in applicants if a not in cancelled]", "active = []")], pols=("P3",)),  # 너무 넓음
    _ch("C5", [("i < capacity", "i <= capacity")], notice="seat_status 를 P5 기준으로"),                   # 결함과 같음 + 금지어
    _ch("C6", [("없는 문자열", "x")]),
]


def test_check_changes(monkeypatch):
    b = _bundle(monkeypatch)
    valid, errs = change.check_changes(b, PolicyChangesOut(changes=GOOD + BAD))
    assert [v["id"] for v in valid] == ["C1", "C2"]
    c1 = valid[0]
    assert c1["chunks"] == ["seat_status"] and c1["policies"] == ["P5"]
    assert c1["examples"] == [{"eid": "P5.1", "policy_id": "P5", "old": "대기 1번", "new": "대기"}]
    assert valid[1]["examples"][0]["new"] == "2026-05-12"
    msg = " | ".join(errs)
    assert "C3: 고친 구현에서 값이 달라지는" in msg and "C4: 예시 3/5개" in msg
    assert "C5: 공지에 쓰면 안 되는 말 seat_status" in msg and "C5: 공지에 정책 id" in msg and "C5: 이미 있는 결함과 같은" in msg
    assert "C6: 치환 1" in msg


def test_generate_changes_accumulates(monkeypatch):
    b = _bundle(monkeypatch)
    calls = []
    outs = iter([PolicyChangesOut(changes=[GOOD[0]] + BAD[:2]), PolicyChangesOut(changes=GOOD)])

    def fake(**kw):
        calls.append(kw["system"])
        return next(outs)
    monkeypatch.setattr(llm, "parse", fake)
    monkeypatch.setattr(change, "CHANGES", (2, 4))
    changes, attempts = change.generate_changes(b)
    assert [c["id"] for c in changes] == ["C1", "C2"] and len(changes) == 2   # 같은 코드는 하나만
    assert len(calls) == 2 and "C3: 고친 구현에서" in calls[1] and "이미 통과한 변경" in calls[1]
    assert attempts[0]["valid"] == ["변경 C1"]

    monkeypatch.setattr(llm, "parse", lambda **kw: PolicyChangesOut(changes=BAD))
    with pytest.raises(change.ChangeError):
        change.generate_changes(b)


def test_change_api_flow(monkeypatch):
    b = _bundle(monkeypatch)
    monkeypatch.setattr(llm, "parse", lambda **kw: PolicyChangesOut(changes=GOOD))
    monkeypatch.setattr(change, "CHANGES", (2, 4))
    monkeypatch.setattr(main, "settings", dataclasses.replace(settings, model_strong="test-model"))
    v = client.post("/api/change/responses", json={"bundle_id": b["bundle_id"]}).json()
    rid = v["response_id"]
    assert len(policy.load(b["bundle_id"])["changes"]) == 2
    q = v["question"]
    assert q["notice"] and len(q["examples"]) == 5 and {c["name"] for c in q["chunks"]} == {"work_date", "seat_status"}
    text = client.get(f"/api/change/responses/{rid}").text
    for secret in ("\"new\"", "대기\"", "edits", "start.hour >= 22"):
        assert secret not in text
    # 1번(C1): 정책 맞음, 예시 하나 더 고름, 값 맞음, 코드 맞음
    r = client.post(f"/api/change/responses/{rid}/cards/1/answer",
                    json={"policies": ["P5"], "examples": {"P5.1": "대기", "P3.1": "확정"}, "chunks": ["seat_status"]}).json()
    s = r["result"]
    assert s["policies"]["exact"] and s["code"]["exact"] and s["values_ok"] == 1
    assert (s["examples"]["hit"], s["examples"]["extra"], s["examples"]["precision"]) == (1, 1, 50) and not s["all_correct"]
    rows = {e["eid"]: e for e in r["examples"]}
    assert rows["P5.1"]["value_ok"] and not rows["P3.1"]["truth"]
    assert client.post(f"/api/change/responses/{rid}/cards/1/answer", json={}).status_code == 409
    assert client.post(f"/api/change/responses/{rid}/cards/2/answer", json={"policies": ["P99"]}).status_code == 409
    # 2번(C2): 다 맞음
    r = client.post(f"/api/change/responses/{rid}/cards/2/answer",
                    json={"policies": ["P1"], "examples": {"P1.1": "2026-05-12"}, "chunks": ["work_date"]}).json()
    assert r["result"]["all_correct"] and r["done"] and any(l["hit"] for sn in r["new_code"] for l in sn["lines"])
    sm = client.get(f"/api/change/responses/{rid}").json()["summary"]
    assert (sm["all_correct"], sm["values_ok"], sm["values_total"], sm["example_precision"]) == (1, 2, 2, 75)
    # 두 번째 풀이는 출제를 다시 하지 않는다
    monkeypatch.setattr(llm, "parse", lambda **kw: pytest.fail("다시 출제함"))
    assert client.post("/api/change/responses", json={"bundle_id": b["bundle_id"]}).status_code == 200
    assert client.get("/api/change").json()["bundles"][0]["responses"] == 2
    assert client.get("/api/review").json()["bundles"][0]["drills"] == ["D"]


def test_change_readers_not_penalized():
    code = "CAP = {'a': 2}\n\n\ndef seats(route):\n    return CAP[route]\n"
    b = {"reference_code": code}
    ch = {"chunks": ["CAP"]}
    assert change._readers(b, ch) == ["seats"]
