"""정책 묶음 엔진 + B(성공 기준) 드릴. LLM은 대체하고, 정답 구현·결함 버전은 하네스로 실제 실행한다."""
import shutil

import pytest
from fastapi.testclient import TestClient

from app import criteria, llm, policy, policy_harness
from app.config import settings
from app.main import app
from app.schemas import PolicyBundleOut

client = TestClient(app)

REF = '''from datetime import timedelta


def work_date(start, end):
    d = start.date()
    if start.hour < 6:
        d -= timedelta(days=1)
    return d


def seat_status(capacity, applicants, cancelled, who):
    active = [a for a in applicants if a not in cancelled]
    if who not in active:
        return "없음"
    i = active.index(who)
    if i < capacity:
        return "확정"
    return f"대기 {i - capacity + 1}번"
'''

Q1 = "근무의 업무 기준일은 출근한 날이며, 퇴근이 다음 날이어도 바뀌지 않는다."
Q2 = "단, 출근 시각이 00:00~05:59이면 전날 근무로 본다."
Q3 = "정원 안에 든 신청자는 신청 순서대로 확정 처리한다."
Q4 = "취소자가 생기면 대기자가 신청 순서대로 올라간다."
Q5 = "정원과 같은 순번까지 확정이며 그다음부터 대기 1번이다."
DOC = f"# 셔틀 운영 기준\n\n## 3. 업무 정책\n{Q1}\n{Q2}\n\n## 4. 좌석\n{Q3}\n{Q4}\n\n## 부록\n{Q5}\n"


def _ex(func, expected, note="", **args):
    return {"func": func, "args": [{"name": k, "value": v} for k, v in args.items()], "expected": expected, "note": note}


def _wd(s, e, exp):
    return _ex("work_date", exp, start=s, end=e)


def _seat(cap, apps, canc, who, exp):
    return _ex("seat_status", exp, capacity=cap, applicants=apps, cancelled=canc, who=who)


def bundle_dict(**over) -> dict:
    d = {
        "title": "셔틀 운영 기준",
        "spec_doc": DOC,
        "functions": [
            {"name": "work_date", "summary": "근무 한 건의 업무 기준일",
             "params": [{"name": "start", "label": "출근", "type": "datetime", "options": []},
                        {"name": "end", "label": "퇴근", "type": "datetime", "options": []}],
             "returns": {"label": "업무 기준일", "type": "date", "options": []}},
            {"name": "seat_status", "summary": "신청자 한 명의 좌석 상태 ('확정', '대기 N번', '없음')",
             "params": [{"name": "capacity", "label": "정원", "type": "int", "options": []},
                        {"name": "applicants", "label": "신청 순서", "type": "list", "options": []},
                        {"name": "cancelled", "label": "취소자", "type": "list", "options": []},
                        {"name": "who", "label": "확인할 사람", "type": "str", "options": []}],
             "returns": {"label": "상태", "type": "str", "options": []}},
        ],
        "policies": [
            {"id": "P1", "title": "야간 근무 기준일", "statement": "출근일 기준", "location": "3. 업무 정책",
             "doc_quote": Q1, "kind": "counterintuitive",
             "examples": [_wd("2026-05-13 22:00", "2026-05-14 07:00", "2026-05-13")]},
            {"id": "P2", "title": "새벽 출근", "statement": "06시 전 출근은 전날", "location": "3. 업무 정책 단서",
             "doc_quote": Q2, "kind": "counterintuitive",
             "examples": [_wd("2026-05-14 05:59", "2026-05-14 14:00", "2026-05-13")]},
            {"id": "P3", "title": "정원 내 확정", "statement": "정원 안이면 확정", "location": "4. 좌석",
             "doc_quote": Q3, "kind": "general", "examples": [_seat("2", "a, b, c", "", "b", "확정")]},
            {"id": "P4", "title": "취소 시 승격", "statement": "취소하면 대기자 승격", "location": "4. 좌석",
             "doc_quote": Q4, "kind": "conflict", "examples": [_seat("2", "a, b, c", "a", "c", "확정")]},
            {"id": "P5", "title": "정원 경계", "statement": "정원 번째까지 확정", "location": "부록",
             "doc_quote": Q5, "kind": "constraint", "examples": [_seat("2", "a, b, c", "", "c", "대기 1번")]},
        ],
        "reference_code": REF,
        "defects": [
            {"id": "D1", "policy_id": "P1", "summary": "퇴근 날짜로 기록된다",
             "edits": [{"find": "    d = start.date()", "replace": "    d = end.date()"}]},
            {"id": "D2", "policy_id": "P2", "summary": "05시대 출근이 당일로 잡힌다",
             "edits": [{"find": "start.hour < 6", "replace": "start.hour < 5"}]},
            {"id": "D3", "policy_id": "P4", "summary": "취소해도 대기자가 안 올라간다",
             "edits": [{"find": "active = [a for a in applicants if a not in cancelled]", "replace": "active = list(applicants)"}]},
            {"id": "D4", "policy_id": "P5", "summary": "정원보다 한 명 더 확정된다",
             "edits": [{"find": "i < capacity", "replace": "i <= capacity"}]},
        ],
    }
    d.update(over)
    return d


@pytest.fixture(autouse=True)
def limits(monkeypatch):
    monkeypatch.setattr(policy, "DOC_LEN", {"target": (100, 200), "accept": (50, 5000)})
    monkeypatch.setattr(policy, "POLICIES", (4, 12))
    monkeypatch.setattr(policy, "FUNCTIONS", (2, 5))
    monkeypatch.setattr(policy, "DEFECTS", (3, 7))
    for kind in ("bundles", "sheets"):
        shutil.rmtree(settings.data_dir / "drills" / kind, ignore_errors=True)


def test_harness_parse_canon():
    p = policy_harness.parse
    assert p("datetime", "2026-05-13T22:00").hour == 22 and p("time", "7:00").hour == 7
    assert p("list", "a, b,, c") == ["a", "b", "c"] and p("bool", "참") is True and p("int", "1,500") == 1500
    assert policy_harness.expected_canon("number", "1500.0") == policy_harness.canon(1500) == "1500"
    assert policy_harness.canon(["a", "b"]) == "a, b" and policy_harness.canon("대기  1번 ") == "대기 1번"
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        p("date", "5/13")
    with pytest.raises(ValueError, match="선택지"):
        p("choice", "보류", ["확정", "대기"])


def test_static_errors():
    assert policy.static_errors(REF) == []
    bad = "import os\nfrom datetime import datetime\ndef f(x):\n    open('x')\n    return datetime.now()\n"
    errs = " ".join(policy.static_errors(bad))
    assert "os" in errs and "open" in errs and "now" in errs


def test_check_passes_and_records_kills():
    errs, ver = policy.check(PolicyBundleOut(**bundle_dict()))
    assert errs == []
    assert ver["examples"] == 5
    assert "P1.1" in ver["kills"]["D1"] and ver["kills"]["D4"] == ["P5.1"]


def test_check_wrong_expected_and_unkillable_defect():
    d = bundle_dict()
    d["policies"][0]["examples"][0]["expected"] = "2026-05-14"   # 출제자가 퇴근 날짜로 착각
    errs, _ = policy.check(PolicyBundleOut(**d))
    assert any("P1.1" in e and "정답 구현 결과" in e for e in errs)

    d = bundle_dict()
    d["defects"][1]["edits"] = [{"find": "start.hour < 6", "replace": "start.hour <= 5"}]  # 같은 동작
    errs, _ = policy.check(PolicyBundleOut(**d))
    assert any(e.startswith("D2:") and "잡을 수 없는" in e for e in errs)


def test_check_structure_errors():
    d = bundle_dict(spec_doc=DOC.replace(Q3, "정원 안이면 확정.") + "\nwork_date 상태 머신")
    errs = " | ".join(policy.check(PolicyBundleOut(**d))[0])
    assert "상태 머신" in errs and "work_date이 나옴" in errs and "P3: doc_quote" in errs
    # 문서·예시가 맞으면 결함 패치를 검사한다
    d = bundle_dict()
    d["defects"][0]["edits"] = [{"find": "return", "replace": "return"}]   # 여러 번 나옴
    d["defects"][3]["policy_id"] = "P4"                                    # 같은 정책 중복
    errs = " | ".join(policy.check(PolicyBundleOut(**d))[0])
    assert "D1: 치환 1" in errs and "다른 정책" in errs


def test_generate_retries_with_feedback(monkeypatch):
    bad = bundle_dict()
    bad["policies"][1]["examples"][0]["expected"] = "2026-05-14"
    outs = iter([PolicyBundleOut(**bad), PolicyBundleOut(**bundle_dict())])
    systems = []

    def fake(**kw):
        systems.append(kw["system"])
        return next(outs)
    monkeypatch.setattr(llm, "parse", fake)
    b = policy.generate(seed=1)
    assert len(systems) == 2 and "P2.1" in systems[1] and "P2.1" not in systems[0]
    assert b["attempts"][0]["errors"] and b["attempts"][1]["errors"] == []
    assert policy.load(b["bundle_id"])["verification"]["kills"]["D1"]
    assert len(b["spec"]["concepts"]) in (2, 3)


def test_pick_spec_rules():
    for seed in range(30):
        s = policy.pick_spec(seed)
        assert s["concepts"][0] in policy.MAIN_CONCEPTS and s["concepts"][-1] in policy.SIDE_CONCEPTS
        heavy = [c for c in s["concepts"] if policy.generator.CONCEPTS[c]["heavy"]]
        assert len(heavy) <= 1
        assert not any(sys_[2] and sys_[1] == s["system"] for sys_ in policy.fresh.SYSTEMS)
    s = policy.pick_spec(3, recent_concepts={"business_date", "capacity_waitlist"})
    assert s["concepts"][0] not in {"business_date", "capacity_waitlist"}


def _saved_bundle(monkeypatch) -> dict:
    monkeypatch.setattr(llm, "parse", lambda **kw: PolicyBundleOut(**bundle_dict()))
    return policy.generate(seed=2)


ROWS = [
    {"func": "work_date", "args": {"start": "2026-05-13T22:00", "end": "2026-05-14T07:00"}, "expected": "2026-05-13", "note": "야간"},
    {"func": "work_date", "args": {"start": "2026-05-13 22:00", "end": "2026-05-14 07:00"}, "expected": "2026-05-14", "note": ""},
    {"func": "work_date", "args": {"start": "2026-05-13 09:00", "end": "2026-05-13 18:00"}, "expected": "2026-05-13", "note": ""},
    {"func": "seat_status", "args": {"capacity": "2", "applicants": "a, b, c", "cancelled": "", "who": "c"}, "expected": "대기 1번", "note": ""},
    {"func": "work_date", "args": {"start": "5/13 22시", "end": "2026-05-14 07:00"}, "expected": "2026-05-13", "note": ""},
]


def test_grade(monkeypatch):
    b = _saved_bundle(monkeypatch)
    r = criteria.grade(b, criteria._clean_rows(b, ROWS))
    rows = r["rows"]
    assert [x["verdict"] for x in rows] == ["correct", "wrong", "correct", "correct", "invalid"]
    assert rows[0]["kills"] == ["D1"] and rows[2]["kills"] == [] and rows[3]["kills"] == ["D4"]
    assert rows[1]["actual"] == "2026-05-13" and rows[1]["same_as"] == ["D1"]
    assert rows[1]["diagnosis"][0]["policy_id"] == "P1" and "3. 업무 정책" in rows[1]["diagnosis"][0]["location"]
    assert "YYYY-MM-DD" in rows[4]["error"]
    s = r["summary"]
    assert (s["valid"], s["correct"], s["accuracy"], s["killed"], s["coverage"], s["idle"]) == (4, 3, 75, 2, 50, 1)
    assert s["misread"] == ["P1"]
    missed = {d["id"]: d for d in r["defects"] if not d["killed_by"]}
    assert set(missed) == {"D2", "D3"}
    assert missed["D2"]["model_examples"][0]["expected"] == "2026-05-13"
    assert missed["D3"]["policy_title"] == "취소 시 승격"


def test_sheet_api_flow(monkeypatch):
    b = _saved_bundle(monkeypatch)
    ov = client.get("/api/criteria").json()
    assert ov["bundles"][0]["bundle_id"] == b["bundle_id"] and ov["bundles"][0]["defects"] == 4
    v = client.post("/api/criteria/sheets", json={"bundle_id": b["bundle_id"], "seconds": 600}).json()
    sid = v["sheet_id"]
    assert v["status"] == "in_progress" and 590 <= v["remaining_sec"] <= 600
    # 제출 전에는 정책·정답 구현·결함이 응답에 없다
    text = client.get(f"/api/criteria/sheets/{sid}").text
    for secret in ("reference_code", "doc_quote", "P1", "D1", "퇴근 날짜로", "timedelta"):
        assert secret not in text
    assert client.put(f"/api/criteria/sheets/{sid}/rows", json={"rows": ROWS[:2]}).json() == {"saved": 2}
    assert len(client.get(f"/api/criteria/sheets/{sid}").json()["rows"]) == 2
    bad = client.put(f"/api/criteria/sheets/{sid}/rows", json={"rows": [{**ROWS[0], "func": "nope"}]})
    assert bad.status_code == 409
    done = client.post(f"/api/criteria/sheets/{sid}/submit", json={"rows": ROWS}).json()
    assert done["status"] == "done" and done["result"]["summary"]["coverage"] == 50 and not done["timed_out"]
    assert client.post(f"/api/criteria/sheets/{sid}/submit", json={"rows": ROWS}).status_code == 409
    ov = client.get("/api/criteria").json()
    assert ov["sheets"][0]["coverage"] == 50 and ov["bundles"][0]["best_coverage"] == 50
    assert client.get("/api/criteria/sheets/c_20260101_000000_abcd").status_code == 404
