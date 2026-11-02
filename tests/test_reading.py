"""A. 읽기·정리 드릴. LLM 채점은 대체하고, 항목 분리·판정 정리·점수·API 흐름을 확인한다."""
import dataclasses
import shutil

import pytest
from fastapi.testclient import TestClient

from app import llm, main, policy, reading
from app.config import settings
from app.schemas import PolicyBundleOut, ReadingGradeOut
from tests.test_policy import bundle_dict

client = TestClient(main.app)

TEXT = """# 날짜
- 업무 기준일은 출근한 날 (퇴근이 다음 날이어도)
- 새벽 출근은 당일로 본다
# 좌석
1. 정원까지 신청 순서대로 확정
2. 취소하면 대기자가 올라감
3. 대기자는 매일 초기화된다

질문: 동명이인은?
"""


def _judged(**over) -> ReadingGradeOut:
    d = {
        "policies": [
            {"policy_id": "P1", "verdict": "covered", "items": [1], "comment": "무시됨"},
            {"policy_id": "P2", "verdict": "misread", "items": [2], "comment": "06시 전 출근은 전날"},
            {"policy_id": "P3", "verdict": "covered", "items": [3], "comment": ""},
            {"policy_id": "P4", "verdict": "partial", "items": [4], "comment": "순서가 빠짐"},
            {"policy_id": "P9", "verdict": "covered", "items": [1], "comment": ""},     # 없는 정책 → 버림
            # P5 판정 없음 → 누락
        ],
        "items": [
            {"n": 1, "verdict": "matched", "comment": ""}, {"n": 2, "verdict": "matched", "comment": ""},
            {"n": 3, "verdict": "matched", "comment": ""}, {"n": 4, "verdict": "matched", "comment": ""},
            {"n": 5, "verdict": "unsupported", "comment": "문서에 없음"},
            {"n": 99, "verdict": "contradicts", "comment": ""},                       # 없는 번호 → 버림
            # 6번 판정 없음 → 채점 외
        ],
        "overall": "단서 조항을 건너뜀",
    }
    d.update(over)
    return ReadingGradeOut(**d)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(policy, "DOC_LEN", {"target": (100, 200), "accept": (50, 5000)})
    monkeypatch.setattr(policy, "POLICIES", (4, 12))
    monkeypatch.setattr(policy, "FUNCTIONS", (2, 5))
    monkeypatch.setattr(policy, "DEFECTS", (3, 7))
    for kind in ("bundles", "sheets", "reads"):
        shutil.rmtree(settings.data_dir / "drills" / kind, ignore_errors=True)


def _bundle(monkeypatch) -> dict:
    monkeypatch.setattr(llm, "parse", lambda **kw: PolicyBundleOut(**bundle_dict()))
    return policy.generate(seed=2)


def test_parse_items():
    items = reading.parse_items(TEXT)
    assert [i["text"] for i in items] == [
        "업무 기준일은 출근한 날 (퇴근이 다음 날이어도)", "새벽 출근은 당일로 본다", "정원까지 신청 순서대로 확정",
        "취소하면 대기자가 올라감", "대기자는 매일 초기화된다", "질문: 동명이인은?"]
    assert items[0]["section"] == "날짜" and items[3]["section"] == "좌석" and items[5]["n"] == 6
    assert reading.parse_items("#\n- \n\n") == []


def test_grade_normalizes_llm_output(monkeypatch):
    b = _bundle(monkeypatch)
    items = reading.parse_items(TEXT)
    r = reading.grade(b, items, _judged())
    pols = {p["id"]: p for p in r["policies"]}
    assert [p["id"] for p in r["policies"]] == ["P1", "P2", "P3", "P4", "P5"]
    assert pols["P1"]["verdict"] == "covered" and pols["P1"]["comment"] == ""
    assert pols["P2"]["verdict_label"] == "잘못 해석" and pols["P2"]["items"][0]["text"] == "새벽 출근은 당일로 본다"
    assert pols["P2"]["if_missed"] == "05시대 출근이 당일로 잡힌다" and pols["P3"]["if_missed"] is None
    assert pols["P5"]["verdict"] == "missed" and pols["P5"]["items"] == [] and "부록" in pols["P5"]["location"]
    assert [i["verdict"] for i in r["items"]] == ["matched"] * 4 + ["unsupported", "extra"]
    s = r["summary"]
    assert (s["covered"], s["partial"], s["misread"], s["missed"]) == (2, 1, 1, 1)
    assert s["recall"] == 50 and s["unsupported"] == 1 and s["contradicts"] == 0 and s["extra"] == 1
    kinds = {k["kind"]: (k["covered"], k["total"]) for k in s["by_kind"]}
    assert kinds == {"general": (1, 1), "counterintuitive": (1, 2), "constraint": (0, 1), "conflict": (0, 1)}


def test_grade_rejects_verdict_without_evidence(monkeypatch):
    b = _bundle(monkeypatch)
    j = _judged(policies=[{"policy_id": "P1", "verdict": "covered", "items": [42], "comment": ""}])
    r = reading.grade(b, reading.parse_items(TEXT), j)
    assert r["policies"][0]["verdict"] == "missed"
    assert reading.grade(b, [], None)["summary"]["missed"] == 5


def test_prompt_contains_policies_and_items(monkeypatch):
    b = _bundle(monkeypatch)
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return _judged()
    monkeypatch.setattr(llm, "parse", fake)
    reading.judge(b, reading.parse_items(TEXT), 1500)
    sysm = seen["system"]
    assert "25분 안에" in sysm and "P2 [직관에 어긋남]" in sysm and "4. [좌석] 취소하면 대기자가 올라감" in sysm
    assert b["spec_doc"].strip()[:20] in sysm and seen["schema"] is ReadingGradeOut


def test_read_api_flow(monkeypatch):
    b = _bundle(monkeypatch)
    ov = client.get("/api/reading").json()
    assert ov["bundles"][0]["bundle_id"] == b["bundle_id"] and not ov["bundles"][0]["seen"]
    v = client.post("/api/reading/sheets", json={"bundle_id": b["bundle_id"], "seconds": 600}).json()
    rid = v["read_id"]
    assert v["status"] == "in_progress" and 590 <= v["remaining_sec"] <= 600
    text = client.get(f"/api/reading/sheets/{rid}").text
    for secret in ("doc_quote", "P1", "D1", "퇴근 날짜로", "statement"):
        assert secret not in text
    assert client.put(f"/api/reading/sheets/{rid}/text", json={"text": TEXT}).json() == {"saved": 6}
    assert client.get(f"/api/reading/sheets/{rid}").json()["text"] == TEXT

    # 채점 실패: 답안은 남고 진행 중으로 남아 다시 낼 수 있다
    def boom(**kw):
        raise llm.LLMError("timeout")
    monkeypatch.setattr(llm, "parse", boom)
    monkeypatch.setattr(main, "settings", dataclasses.replace(settings, model_strong="test-model"))
    r = client.post(f"/api/reading/sheets/{rid}/submit", json={"text": TEXT})
    assert r.status_code == 502 and "답안은 저장됨" in r.json()["detail"]
    assert client.get(f"/api/reading/sheets/{rid}").json()["status"] == "in_progress"

    monkeypatch.setattr(llm, "parse", lambda **kw: _judged())
    done = client.post(f"/api/reading/sheets/{rid}/submit", json={"text": TEXT}).json()
    assert done["status"] == "done" and done["result"]["summary"]["recall"] == 50
    assert done["result"]["policies"][1]["doc_quote"]          # 제출 후에는 정책이 보인다
    assert client.post(f"/api/reading/sheets/{rid}/submit", json={"text": TEXT}).status_code == 409
    assert client.put(f"/api/reading/sheets/{rid}/text", json={"text": "x"}).status_code == 409
    ov = client.get("/api/reading").json()
    assert ov["reads"][0]["recall"] == 50 and ov["reads"][0]["misread"] == 1 and ov["reads"][0]["invented"] == 1
    assert ov["bundles"][0]["best_recall"] == 50 and ov["bundles"][0]["seen"]
    assert client.get("/api/reading/sheets/r_20260101_000000_abcd").status_code == 404


def test_limits():
    with pytest.raises(reading.ReadingError):
        reading._check_text("- a\n" * (reading.MAX_ITEMS + 1))
    with pytest.raises(reading.ReadingError):
        reading._check_text("a" * (reading.MAX_CHARS + 1))


def test_pause_excludes_stopped_time(monkeypatch):
    """멈춘 동안은 남은 시간·소요 시간에서 빠지고, 정지·재개를 겹쳐 불러도 한 번으로 센다."""
    b = _bundle(monkeypatch)
    rid = reading.create(b["bundle_id"], 600)["read_id"]
    sheet = reading.load(rid)
    sheet["started_at"] = "2026-01-01T09:00:00+09:00"
    reading.cards._write("reads", rid, sheet)
    times = iter(["2026-01-01T09:02:00+09:00", "2026-01-01T09:30:00+09:00"])

    class FakeDT(reading.datetime):
        @classmethod
        def now(cls, tz=None):
            return reading.datetime.fromisoformat(current)
    monkeypatch.setattr(reading, "datetime", FakeDT)
    monkeypatch.setattr(reading.sessions, "now_iso", lambda: current)

    current = next(times)                       # 2분 풀고 정지
    v = client.post(f"/api/reading/sheets/{rid}/pause", json={"paused": True}).json()
    assert v["paused"] and v["remaining_sec"] == 480 and v["pauses"] == 1
    assert client.post(f"/api/reading/sheets/{rid}/pause", json={"paused": True}).json()["pauses"] == 1
    current = next(times)                       # 28분 멈춰 있다가 재개
    assert client.get(f"/api/reading/sheets/{rid}").json()["remaining_sec"] == 480
    v = client.post(f"/api/reading/sheets/{rid}/pause", json={"paused": False}).json()
    assert not v["paused"] and v["remaining_sec"] == 480
    current = "2026-01-01T09:33:00+09:00"       # 3분 더 풀고 제출 → 소요 5분, 시간 초과 아님
    monkeypatch.setattr(llm, "parse", lambda **kw: _judged())
    monkeypatch.setattr(main, "settings", dataclasses.replace(settings, model_strong="test-model"))
    done = client.post(f"/api/reading/sheets/{rid}/submit", json={"text": TEXT}).json()
    assert done["elapsed_sec"] == 300 and not done["timed_out"] and done["pauses"] == 1 and not done["paused"]
    assert client.post(f"/api/reading/sheets/{rid}/pause", json={"paused": True}).status_code == 409


def test_cannot_pause_after_time_is_up(monkeypatch):
    b = _bundle(monkeypatch)
    rid = reading.create(b["bundle_id"], 60)["read_id"]
    sheet = reading.load(rid)
    sheet["started_at"] = "2020-01-01T09:00:00+09:00"
    reading.cards._write("reads", rid, sheet)
    assert client.post(f"/api/reading/sheets/{rid}/pause", json={"paused": True}).status_code == 409
