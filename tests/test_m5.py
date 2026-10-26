import socket
import tarfile
from pathlib import Path

import pytest

from app import evaluate, metrics, runner
from app.schemas import EvalItem

from helpers import load_fixture


# ---------- 자동 실행 ----------

def _snapshot(tmp_path, files: dict) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for k, v in files.items():
        (src / k).write_text(v)
    tar = tmp_path / "snap.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        for k in files:
            t.add(src / k, arcname=k)
    return tar


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_run_app_success(tmp_path):
    tar = _snapshot(tmp_path, {"index.html": "<h1>hello</h1>"})
    port = _free_port()
    r = runner.run_app(tar, f"python3 -m http.server {port} --bind 127.0.0.1", port, tmp_path)
    assert r["started"] and r["http_status"] == 200 and "hello" in r["body_head"]
    assert not runner._port_open(port)  # 종료됨
    assert r["tests"]["found"] is False


def test_run_app_wrong_command_logs_error(tmp_path):
    tar = _snapshot(tmp_path, {"main.py": "print('x')"})
    port = _free_port()
    r = runner.run_app(tar, "python3 nope.py", port, tmp_path)
    assert not r["started"] and r["exit_code"] not in (None, 0)
    assert "nope.py" in r["log_tail"]


def test_run_app_port_busy(tmp_path):
    tar = _snapshot(tmp_path, {"a.txt": "x"})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); s.listen()
        with pytest.raises(runner.PortBusy):
            runner.run_app(tar, "true", s.getsockname()[1], tmp_path)


def test_run_tests_detected(tmp_path):
    (tmp_path / "test_a.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    r = runner.run_tests(tmp_path)
    assert r["found"] and r["exit_code"] == 0


def test_log_masks_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live" + "x" * 20)
    assert "sk-live" not in runner._mask("key=sk-live" + "x" * 20)


# ---------- 정량 지표 ----------

README = load_fixture()[0].readme_markdown


def trace(prompts, timeline=()):
    return {"tool": "codex", "started_at": prompts[0][0], "files_edited": [],
            "user_prompts": [{"ts": ts, "text": t, "elapsed_sec": i * 60} for i, (ts, t) in enumerate(prompts)],
            "token_timeline": [{"ts": ts, "billable": b} for ts, b in timeline]}


def test_readme_paste_and_regeneration_detected(tmp_path):
    t = trace([("2026-10-24T01:00:00Z", README + "\n이거 만들어줘"),
               ("2026-10-24T01:10:00Z", "에세이 제출 폼 추가해줘"),
               ("2026-10-24T01:20:00Z", "맘에 안 들어. 처음부터 다시 만들어줘"),
               ("2026-10-24T01:30:00Z", "pytest 돌려서 확인해줘")],
              [("2026-10-24T01:05:00Z", 5000), ("2026-10-24T01:15:00Z", 9000),
               ("2026-10-24T01:25:00Z", 90000), ("2026-10-24T01:35:00Z", 95000)])
    _, sealed = load_fixture()
    m = metrics.compute(readme=README, sealed=sealed, chat=[], process={"agent_traces": [t], "commits": [], "docs": {}},
                        workspace=tmp_path)
    a = m["agent"]
    assert len(a["readme_paste"]) == 1 and a["readme_paste"][0]["overlap"] >= 0.7
    assert [p["text"] for p in a["full_regeneration"]] == ["맘에 안 들어. 처음부터 다시 만들어줘"]
    assert [p["tokens"] for p in a["token_spikes"]] == [81000]
    assert a["verification_prompts"] == 1


def _cmd(sec, cmd, ok, out=""):
    return {"ts": None, "cmd": cmd, "ok": ok, "elapsed_sec": sec, "output_tail": out}


def test_command_kind_ignores_search_and_install():
    assert metrics.command_kind("cd app && timeout 120 uv run pytest -q 2>&1 | tail -3") == "test"
    assert metrics.command_kind("CI=1 npm run test") == "test"
    assert metrics.command_kind('grep -rn "pytest" docs | head') is None
    assert metrics.command_kind("pip install pytest") is None
    assert metrics.command_kind("cat tests/test_api.py") is None
    assert metrics.command_kind("curl -s http://127.0.0.1:8000/health") == "smoke"


def test_outcome_reads_summary_through_pipe():
    # `pytest | tail`은 종료 코드 0이어도 출력 요약 줄이 실패면 실패
    assert metrics.test_outcome(_cmd(0, "pytest -q | tail -2", True, "1 failed, 9 passed in 0.5s")) is False
    assert metrics.test_outcome(_cmd(0, "pytest -q", False, "10 passed in 0.5s")) is True
    assert metrics.test_outcome(_cmd(0, "npm test", False, "")) is False  # 요약 줄이 없으면 종료 상태


def test_verification_runs_timeline_and_results():
    traces = [{"commands": [_cmd(600, "pip install pytest", True), _cmd(900, "cat main.py", True),
                            _cmd(1200, "uv run pytest -q", False), _cmd(1500, "pytest tests/test_api.py", True),
                            _cmd(1600, "curl -s localhost:8000/health", True)]}]
    commits = [{"hash": "init", "stat": " README.md | 3 +++", "elapsed_sec": -10},
               {"hash": "a", "stat": " main.py | 40 ++++", "elapsed_sec": 1550},
               {"hash": "b", "stat": " main.py | 5 ++", "elapsed_sec": 3000},
               {"hash": "c", "stat": " NOTES.md | 5 ++", "elapsed_sec": 3100}]
    v = metrics.verification_runs(traces, commits)
    assert (v["test_runs"], v["test_passed"], v["test_failed"]) == (2, 1, 1)  # 설치 명령은 제외
    assert v["fail_then_pass"] and v["first_test_elapsed_sec"] == 1200
    assert v["last_test"] == {"elapsed_sec": 1500, "ok": True}
    assert v["code_commits_after_last_pass"] == 2  # 문서만 바꾼 커밋은 세지 않음
    assert v["smoke_checks"] == 1 and [r["kind"] for r in v["timeline"]] == ["test", "test", "smoke"]
    assert metrics.verification_runs([{"user_prompts": []}], [])["test_runs"] == 0  # 이전 process.json 호환


def test_commit_sizes_flags_big_bang_and_final_window():
    commits = [{"hash": "init", "stat": " README.md | 900 +++", "elapsed_sec": -10},
               {"hash": "a", "stat": " main.py | 20 ++\n package-lock.json | 5000 +++", "elapsed_sec": 1800},
               {"hash": "b", "stat": " main.py | 30 ++\n app.js | 400 +++\n 2 files changed", "elapsed_sec": 10000}]
    c = metrics.commit_sizes(commits, duration_sec=180 * 60)
    assert c["commits"] == 2 and c["total_lines"] == 450  # 초기 커밋·lock 파일 제외
    assert c["max_commit"] == "b" and c["max_share"] == 0.96 and c["big_bang"]
    assert c["final_30min_share"] == 0.96
    small = [commits[0]] + [{"hash": h, "stat": " main.py | 40 ++", "elapsed_sec": s} for h, s in (("x", 1000), ("y", 4000), ("z", 7000))]
    c = metrics.commit_sizes(small, duration_sec=180 * 60)
    assert not c["big_bang"] and c["final_30min_share"] == 0.0 and c["median_lines"] == 40


def test_paraphrase_is_not_paste():
    assert metrics.readme_overlap(README, "학생이 에세이를 내면 피드백을 주는 서비스를 FastAPI로 만들자") < 0.3


def test_discovery_and_first_impl_commit():
    _, sealed = load_fixture()
    chat = [{"role": "user", "text": "q1"}, {"role": "persona", "disclosed_fact_ids": ["F01"], "partial_fact_ids": ["F02"]},
            {"role": "user", "text": "q2"}, {"role": "persona", "disclosed_fact_ids": ["F01", "F03"]}]
    d = metrics.discovery(sealed, chat)
    assert d["F01"]["question_no"] == 1 and d["F03"]["question_no"] == 2
    assert d["F02"]["partial"] and not d["F02"]["discovered"]
    commits = [{"hash": "a", "stat": " README.md | 3 +++"}, {"hash": "b", "stat": " ACCEPTANCE.md | 2 ++"},
               {"hash": "c", "stat": " main.py | 40 ++++\n 1 file changed"}]
    assert metrics.first_impl_commit(commits)["hash"] == "c"


# ---------- 항목 정리 ----------

def test_normalize_clamps_and_fills_missing():
    items = [EvalItem(key="maintainability", verdict="충족", score=99, rationale="r", evidence=[], suggestion="")]
    out = evaluate.normalize(items, evaluate.TECH_ITEMS)
    assert [i["key"] for i in out] == [k for k, _, _ in evaluate.TECH_ITEMS]
    assert out[0]["score"] == 20 and out[1]["verdict"] == "미충족" and out[1]["score"] == 0


def test_run_failure_caps_functional_correctness():
    items = evaluate.normalize([EvalItem(key="functional_correctness", verdict="충족", score=18, rationale="좋음",
                                         evidence=[], suggestion="")], evaluate.TECH_ITEMS)
    evaluate.apply_run_cap(items, {"started": False, "error": "60초 안에 포트 8000가 열리지 않음"})
    fc = next(i for i in items if i["key"] == "functional_correctness")
    assert fc["verdict"] == "미충족" and fc["score"] == evaluate.RUN_FAIL_CAP and "자동 실행 실패" in fc["rationale"]


def test_intent_max_points_sum_to_100():
    assert sum(mx for _, _, mx in evaluate.INTENT_ITEMS) == 100
    assert sum(mx for _, _, mx in evaluate.TECH_ITEMS) == 100


# ---------- 공개·회고 ----------

def test_reveal_gated_until_evaluated():
    from fastapi.testclient import TestClient
    from app.main import app
    from helpers import make_session
    sid = make_session()
    c = TestClient(app)
    assert c.post(f"/api/sessions/{sid}/reveal").status_code == 409
    assert c.get(f"/api/sessions/{sid}/reveal").status_code == 409
    assert c.put(f"/api/sessions/{sid}/retro", json={"text": "x"}).status_code == 409
    assert c.post(f"/api/sessions/{sid}/evaluate").status_code == 409  # 리허설 전


def test_reveal_and_retro_template(monkeypatch):
    import json
    from app import llm, reveal, sessions
    from app.schemas import MissedFactAnalysis, RevealMissed
    from helpers import make_session
    sid = make_session()
    s = sessions.load_session(sid)
    s.status = "evaluated"
    sessions.save_session(s)
    sessions.append_jsonl(sid, "chat.jsonl", {"ts": "2026-10-24T10:00:00+09:00", "elapsed_sec": 10, "role": "user", "text": "먼저 뭘 보세요?"})
    sessions.append_jsonl(sid, "chat.jsonl", {"ts": "2026-10-24T10:00:05+09:00", "elapsed_sec": 15, "role": "persona", "text": "주장-근거요",
                                              "disclosed_fact_ids": ["F01"], "question_quality": {"specificity": 4}})
    (sessions.session_dir(sid) / "report.json").write_text(json.dumps({
        "scores": {"technical": 50, "intent": 40, "cognitive": 30},
        "intent": {"next_actions": ["A", "B", "C"], "metrics": {"tokens": {"billable": 1234}}}}))
    monkeypatch.setattr(llm, "parse", lambda **kw: RevealMissed(items=[
        MissedFactAnalysis(fact_id=f"F0{i}", closest_user_question="먼저 뭘 보세요?", why_missed="일반적", better_question=f"더 나은 질문 {i}")
        for i in range(2, 7)]))
    rv = reveal.reveal(sid)
    f = {x["id"]: x for x in rv["facts"]}
    assert f["F01"]["discovered"] and f["F01"]["question_no"] == 1 and f["F01"]["chat_turn"] == 2
    assert all(f[i]["better_question"] for i in ("F02", "F03", "F04", "F05", "F06"))
    md = rv["retro_md"]
    assert "기술 50 | 의도 40 | 인지 30 | 토큰 1234" in md
    assert "숨은 사실 발견: 1/6 (critical 1/2)" in md and "더 나은 질문 3" in md and "1. A" in md
    assert sessions.load_session(sid).status == "revealed"
