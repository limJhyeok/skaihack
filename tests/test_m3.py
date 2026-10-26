import json
import subprocess
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import collector, sessions, submit, timer
from app.agent_logs import ClaudeCodeAdapter, CodexAdapter
from app.main import app

from helpers import make_session

FIX = Path(__file__).parent / "fixtures" / "logs"
UTC = timezone.utc


def _install_log(src: str, dest: Path, ws: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text((FIX / src).read_text(encoding="utf-8").replace("__WS__", str(ws)), encoding="utf-8")
    return dest


def _window():
    return datetime(2026, 10, 24, 12, 0, tzinfo=UTC), datetime(2026, 10, 24, 14, 0, tzinfo=UTC)


# ---------- 로그 어댑터 ----------

def test_codex_adapter(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    root = tmp_path / "codex"
    p = _install_log("codex_rollout.jsonl", root / "2026/10/24/rollout-x.jsonl", ws)
    _install_log("codex_rollout.jsonl", root / "2026/10/24/rollout-other.jsonl", tmp_path / "elsewhere")
    a = CodexAdapter(root)
    start, end = _window()
    import os; os.utime(p, None)
    assert a.find_sessions(ws, start, end) == [p]
    assert a.find_sessions(ws, end + timedelta(days=1), end + timedelta(days=2)) == []
    t = a.parse(p)
    assert [x["text"] for x in t.user_prompts] == ["NOTES.md 파일을 만들고 README를 한 줄로 요약해서 써줘"]
    assert t.tool_calls_count == 2
    assert t.files_edited == [f"{ws}/NOTES.md"]
    assert t.agent_messages == []  # 픽스처에는 AgentMessage 없음
    assert [(c["cmd"], c["exit_code"], c["ok"]) for c in t.commands] == [("cat README.md", 0, True), ("uv run pytest -q", 1, False)]
    assert [x["billable"] for x in t.token_timeline] == [15290 - 12288 + 77, 3897]
    assert t.token_usage == {"input": 62029, "cached_input": 58368, "output": 236, "total": 62265, "billable": 3897}


def test_claude_adapter(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    root = tmp_path / "claude"
    p = _install_log("claude_session.jsonl", root / "-proj" / "abc.jsonl", ws)
    a = ClaudeCodeAdapter(root)
    start, end = _window()
    assert a.find_sessions(ws, start, end) == [p]
    t = a.parse(p)
    assert [x["text"] for x in t.user_prompts] == ["README 읽고 계획 세워줘", "테스트도 돌려줘"]
    assert t.tool_calls_count == 3
    assert t.files_edited == [f"{ws}/PLAN.md"]
    # Bash 결과의 is_error로 성공 여부 판정
    assert [(c["cmd"], c["ok"]) for c in t.commands] == [("ls", True), ("pytest -q", False)]
    # msg_1은 두 줄이지만 한 번만 합산
    assert t.token_usage["output"] == 160
    assert t.token_usage["billable"] == 2 + 1000 + 100 + 3 + 0 + 50 + 1 + 10
    assert [x["billable"] for x in t.token_timeline] == [1102, 1155, 1166]


# ---------- 타이머 ----------

def _set(sid, **kw):
    s = sessions.load_session(sid)
    for k, v in kw.items():
        setattr(s, k, v)
    sessions.save_session(s)
    return s


def test_pause_only_in_practice():
    c = TestClient(app)
    full = make_session()
    assert c.post(f"/api/sessions/{full}/pause").status_code == 409
    prac = make_session()
    _set(prac, mode="practice")
    r = c.post(f"/api/sessions/{prac}/pause").json()
    assert r["paused"] is True
    assert c.post(f"/api/sessions/{prac}/chat", json={"text": "질문"}).status_code == 409
    assert c.post(f"/api/sessions/{prac}/resume").json()["paused"] is False


def test_paused_time_is_not_counted():
    sid = make_session()
    t0 = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    s = _set(sid, started_at=t0.isoformat(), duration_min=10, mode="practice")
    for ts, typ in [(t0 + timedelta(minutes=2), "pause"), (t0 + timedelta(minutes=5), "resume")]:
        sessions.append_jsonl(sid, "events.jsonl", {"ts": ts.isoformat(), "type": typ})
    st = timer.state(s, now=t0 + timedelta(minutes=6))
    assert st["active_elapsed_sec"] == 180 and st["remaining_sec"] == 420 and not st["time_over"]
    assert timer.state(s, now=t0 + timedelta(minutes=14))["time_over"]


def test_time_over_blocks_chat():
    sid = make_session()
    _set(sid, started_at=(datetime.now().astimezone() - timedelta(minutes=3)).isoformat(), duration_min=2)
    c = TestClient(app)
    assert c.get(f"/api/sessions/{sid}/timer").json()["time_over"] is True
    r = c.post(f"/api/sessions/{sid}/chat", json={"text": "질문"})
    assert r.status_code == 409 and "시간" in r.json()["detail"]


# ---------- 제출 ----------

FAKE_KEY = "sk-test" + "A1b2C3d4E5f6G7h8I9j0"


@pytest.fixture
def submitted(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README.md").write_text("# 앱\n\n실행:\n```\nuv run uvicorn main:app --port 8123\n```\n`OPENAI_API_KEY=your-key` 형식으로 .env 작성\n")
    (ws / ".env.example").write_text("OPENAI_API_KEY=\nOPENAI_BASE_URL=\n")
    (ws / ".gitignore").write_text(".env\n")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "initial: problem files"], cwd=ws, check=True)
    (ws / ".env").write_text(f"OPENAI_API_KEY={FAKE_KEY}\n")
    (ws / "main.py").write_text(f'import os\nclient_key = "{FAKE_KEY}"\nok = os.environ["OPENAI_API_KEY"]\n')
    (ws / "node_modules").mkdir(); (ws / "node_modules" / "x.js").write_text("x")

    collector.save_log_paths({"codex_sessions_dir": str(tmp_path / "none1"), "claude_projects_dir": str(tmp_path / "none2")})
    sid = make_session()
    _set(sid, workspace_path=str(ws))
    c = TestClient(app)
    info = c.get(f"/api/sessions/{sid}/submit-info").json()
    r = c.post(f"/api/sessions/{sid}/submit", json={
        "run_command": info["suggested_command"], "app_port": info["suggested_port"], "auto_commit": True})
    assert r.status_code == 200, r.text
    yield sid, ws, info, r.json()
    collector.save_log_paths({})


def test_submit_info_suggests_from_readme(submitted):
    _, _, info, _ = submitted
    assert info["suggested_command"] == "uv run uvicorn main:app --port 8123"
    assert info["suggested_port"] == 8123
    assert "main.py" in info["dirty_files"]


def test_suggest_prefers_server_command_over_check(tmp_path):
    """E2E에서 발견: README 마지막 명령(npm run check)을 고르고 포트를 8000으로 제안하던 문제."""
    (tmp_path / "README.md").write_text(
        "```bash\nnpm run demo\nnpm start\nnpm run dev\n```\n브라우저에서 http://127.0.0.1:3000 에 접속\n"
        "```bash\nnpm test\nnpm run check\nnpm ci\n```\n")
    assert submit.suggest_command(tmp_path) == ("npm start", 3000)


def test_secret_scan_ignores_test_placeholder_values(tmp_path):
    (tmp_path / "a.test.js").write_text("const env = { OPENAI_API_KEY: 'test-key' };\nconst e2 = { OPENAI_API_KEY: 'secret' };\n")
    (tmp_path / "b.py").write_text('OPENAI_API_KEY = "abcd1234realvalue"\n')
    found = submit.scan_secrets(tmp_path, ["a.test.js", "b.py"])
    assert [(f["file"], f["kind"]) for f in found if f["file"] != ".env"] == [("b.py", "api_key_assignment")]


def test_submit_tags_snapshots_and_excludes_env(submitted):
    sid, ws, _, res = submitted
    assert res["auto_committed"] is True
    log = subprocess.run(["git", "log", "--format=%s"], cwd=ws, capture_output=True, text=True).stdout.split("\n")
    assert log[0] == "submission snapshot"
    assert subprocess.run(["git", "tag"], cwd=ws, capture_output=True, text=True).stdout.strip() == "submission"
    with tarfile.open(sessions.session_dir(sid) / "snapshot.tar.gz") as tar:
        names = tar.getnames()
    assert ".env" not in names and not any(n.startswith(("node_modules", ".git/")) for n in names)
    assert {"main.py", "README.md", ".env.example"} <= set(names)
    assert sessions.load_session(sid).status == "submitted"


def test_secret_scan_catches_hardcoded_key(submitted):
    sid, _, _, res = submitted
    files = {(f["file"], f["kind"]) for f in res["secret_findings"]}
    assert ("main.py", "openai_key") in files
    assert not any(f["file"] in ("README.md", ".env.example", ".env") for f in res["secret_findings"])
    # 키 원문은 어디에도 저장되지 않음
    d = sessions.session_dir(sid)
    for name in ("submission.json", "process.json", "events.jsonl"):
        assert FAKE_KEY not in (d / name).read_text()


def test_missing_agent_logs_marked_not_collected(submitted):
    sid, _, _, res = submitted
    assert res["agent_status"] == "수집 안 됨" and res["token_source"] == "수집 안 됨"
    process = json.loads((sessions.session_dir(sid) / "process.json").read_text())
    assert [c["subject"] for c in process["commits"]] == ["initial: problem files", "submission snapshot"]


def test_cannot_submit_twice(submitted):
    sid, *_ = submitted
    r = TestClient(app).post(f"/api/sessions/{sid}/submit", json={"run_command": "x", "app_port": 1})
    assert r.status_code == 409
