import json

from fastapi.testclient import TestClient

from app import sessions
from app.main import app

from helpers import make_session


def _finish(sid, scores, disclosed, status="revealed", started="2026-10-20T10:00:00+09:00"):
    s = sessions.load_session(sid)
    s.status, s.started_at = status, started
    sessions.save_session(s)
    sessions.append_jsonl(sid, "chat.jsonl", {"ts": started, "elapsed_sec": 1, "role": "user", "text": "q"})
    sessions.append_jsonl(sid, "chat.jsonl", {"ts": started, "elapsed_sec": 2, "role": "persona", "text": "a",
                                              "disclosed_fact_ids": disclosed, "question_quality": {"specificity": 3}})
    (sessions.session_dir(sid) / "report.json").write_text(json.dumps({
        "scores": scores,
        "intent": {"metrics": {"interview": {"discovery": {"all": {"found": len(disclosed), "total": 6}}},
                               "tokens": {"billable": 1000}}}}))


def test_dashboard_trend_types_and_tips(tmp_path, monkeypatch):
    from app import config
    import dataclasses
    from app import sessions as smod
    monkeypatch.setattr(smod, "settings", dataclasses.replace(config.settings, data_dir=tmp_path))
    a, b, c = make_session(), make_session(), make_session()
    _finish(a, {"technical": 40, "intent": 30, "cognitive": None}, ["F01"], started="2026-10-20T10:00:00+09:00")
    _finish(b, {"technical": 60, "intent": 55, "cognitive": 70}, ["F01", "F02", "F03"], started="2026-10-22T10:00:00+09:00")
    _finish(c, {"technical": 70, "intent": 65, "cognitive": 50}, ["F01", "F04"], status="evaluated",
            started="2026-10-23T10:00:00+09:00")
    d = TestClient(app).get("/api/dashboard").json()
    assert [t["technical"] for t in d["trend"]] == [40, 60, 70]  # 시간순, 평가된 세션 모두
    assert d["trend"][0]["cognitive"] is None
    assert d["sessions"][0]["session_id"] == c  # 최신순
    assert d["sessions"][0]["discovery"] == {"found": 2, "total": 6}
    # 사실 유형 통계는 공개된 세션(a, b)만
    assert d["revealed_sessions"] == 2
    types = {t["type"]: t for t in d["fact_types"]}
    assert types["constraint"]["total"] == 2 and types["constraint"]["found"] == 0  # F04는 공개 안 된 c에서만 발견
    assert types["critical"] == {**types["critical"], "found": 3, "total": 4}
    assert [t["type"] for t in d["tips"]][0] == "constraint"
    assert "제약·금지 사항 질문을 습관화" in d["tips"][0]["tip"]
