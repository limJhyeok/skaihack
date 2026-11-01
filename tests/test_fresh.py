"""새 문제 드릴: 사양 선택, 저장소 검사, 생성 → 테스트 → 카드 검증 흐름. LLM은 대체하고 저장소 테스트·probe는 실제로 실행한다 (uv 필요)."""
import dataclasses
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app import cards, config, fresh, llm
from app.config import settings
from app.main import app
from app.schemas import CardSlot, FreshRepo, GeneratedCard, GeneratedDeck, PlantedDefect, RepoFile

client = TestClient(app)

MAIN = '''from fastapi import FastAPI, HTTPException

app = FastAPI()
CAPACITY = 2
seats: dict[str, list[str]] = {}


def remaining(route: str) -> int:
    return CAPACITY - len(seats.get(route, []))


@app.post("/routes/{route}/book")
def book(route: str, name: str):
    taken = seats.setdefault(route, [])
    if len(taken) > CAPACITY:
        raise HTTPException(409, "full")
    taken.append(name)
    return {"remaining": remaining(route)}
'''
TEST = '''from fastapi.testclient import TestClient
from main import app


def test_book_once():
    r = TestClient(app).post("/routes/A/book", params={"name": "kim"})
    assert r.status_code == 200
'''
FILES = {
    "README.md": "# 셔틀\n\n`uvicorn main:app --host 0.0.0.0 --port 8000`\n",
    "CONTEXT.md": "- [담당자 확정] 노선 정원은 2명\n",
    "DECISIONS.md": "- 메모리 저장\n",
    "requirements.txt": "fastapi\nuvicorn\nhttpx\npytest\n",
    "main.py": MAIN,
    "tests/test_main.py": TEST,
}
SPEC = {"domain": "사내 총무", "system": "셔틀", "external": False, "defects": ["CAPACITY_EDGE"]}


def _repo(files=None, planted=("CAPACITY_EDGE",)):
    files = FILES if files is None else files
    return FreshRepo(title="셔틀 좌석 신청", files=[RepoFile(path=p, content=c) for p, c in files.items()],
                     planted=[PlantedDefect(tag=t, location="main.py:15", behavior="정원이 찬 뒤에도 한 명 더 신청된다") for t in planted],
                     bait=PlantedDefect(tag="BAIT", location="main.py:9", behavior="남은 좌석 계산은 정확하다"))


@pytest.fixture
def ws(tmp_path, monkeypatch):
    s = dataclasses.replace(config.settings, workspace_root=tmp_path / "ws")
    monkeypatch.setattr(fresh, "settings", s)
    shutil.rmtree(settings.data_dir / "drills", ignore_errors=True)
    return s.workspace_root


def test_pick_spec_rules():
    for seed in range(40):
        s = fresh.pick_spec(seed, weights={t: 1 for t in fresh.FRESH_TAGS})
        assert 2 <= len(s["defects"]) <= 3 and len(set(s["defects"])) == len(s["defects"])
        assert any(t in fresh.BUSINESS_TAGS for t in s["defects"])
        if not s["external"]:
            assert "FAIL_ALL_PENDING" not in s["defects"]
    # 자주 틀린 태그가 더 자주 나온다
    heavy = {t: 1 for t in fresh.FRESH_TAGS} | {"TZ_BOUNDARY": 50}
    hits = sum("TZ_BOUNDARY" in fresh.pick_spec(seed, weights=heavy)["defects"] for seed in range(40))
    assert hits >= 30
    recent = {d for d, _, _ in fresh.SYSTEMS[:-1]}
    assert fresh.pick_spec(1, recent, {t: 1 for t in fresh.FRESH_TAGS})["domain"] == fresh.SYSTEMS[-1][0]


def test_check_rules():
    assert fresh.check(_repo(), SPEC) == []
    bad_files = dict(FILES) | {"requirements.txt": "fastapi\nrequests\n", "main.py": MAIN + "\n# TODO 정원 버그\n",
                               "../evil.py": "x"}
    bad_files.pop("CONTEXT.md")
    errs = " | ".join(fresh.check(_repo(bad_files, planted=()), SPEC))
    for needle in ["requests", "TODO", "버그", "잘못된 파일 경로", "CONTEXT.md", "planted에 빠진 결함"]:
        assert needle in errs, needle


def test_generate_repo_retries_on_failing_tests_then_commits(ws, monkeypatch):
    failing = dict(FILES) | {"tests/test_main.py": TEST + "\n\ndef test_fail():\n    assert False\n"}
    outs = [_repo(failing), _repo()]
    prompts_seen = []

    def fake(**kw):
        prompts_seen.append(kw["system"])
        return outs.pop(0)
    monkeypatch.setattr(llm, "parse", fake)
    monkeypatch.setattr(fresh, "pick_spec", lambda seed, recent: SPEC)
    meta = fresh.generate_repo()
    assert "저장소 테스트 실패" in prompts_seen[1]  # 실패 사유 피드백
    root = ws / "drills" / meta["repo_id"]
    assert meta["root"] == str(root) and (root / "main.py").exists()
    assert meta["readme_command"].startswith("uvicorn")
    log = subprocess.run(["git", "log", "--oneline"], cwd=root, capture_output=True, text=True).stdout
    assert log.count("\n") == 1
    assert "CAPACITY_EDGE" not in "".join(p.read_text() for p in root.rglob("*.py"))  # 결함 사양은 저장소 밖


def _card(cid):
    probe_code = '''
import main
from fastapi.testclient import TestClient
def test_p():
    c = TestClient(main.app)
    codes = [c.post("/routes/X/book", params={"name": str(i)}).status_code for i in range(3)]
    observe("s1", codes[2])
    observe("s2", "예" if main.remaining("X") < 0 else "아니오")
'''
    return GeneratedCard(
        id=cid, context="직원이 노선을 골라 셔틀 좌석을 신청하면 서버가 남은 좌석 수를 알려 준다.",
        scenario="정원 두 명인 노선에 세 번째 직원이 신청한다.", evidence=["main.py:13-17"],
        explanation="정원 비교가 초과일 때만 막아서 세 번째 신청도 받아들여진다.", defect_tags=["CAPACITY_EDGE"],
        probe=probe_code,
        slots=[CardSlot(id="s1", prompt="세 번째 신청의 응답 상태 코드는?", kind="value", options=[], claimed="200"),
               CardSlot(id="s2", prompt="남은 좌석이 음수가 되나?", kind="choice", options=["예", "아니오"], claimed="예")])


def test_fresh_api_builds_repo_and_deck(ws, monkeypatch):
    def fake(**kw):
        if kw["purpose"] == "fresh_repo":
            return _repo()
        assert "이 저장소에 심어 둔 결함과 미끼" in kw["system"] and "CAPACITY_EDGE" in kw["system"]
        return GeneratedDeck(cards=[_card("C1"), _card("C2"), _card("C3")])
    monkeypatch.setattr(llm, "parse", fake)
    monkeypatch.setattr(fresh, "pick_spec", lambda seed, recent: SPEC)
    r = client.post("/api/drills/fresh", json={"count": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["generation"]["verified"] == 3
    src = next(s for s in client.get("/api/drills/sources").json()["sources"] if s["key"] == body["repo_id"])
    assert src["kind"] == "fresh" and src["cards"] == 3 and src["path"] == body["root"]

    run = client.post("/api/drills/runs", json={"mode": "open", "count": 1, "session_id": body["repo_id"]}).json()
    q = client.post(f"/api/drills/runs/{run['run_id']}/cards/1/start").json()
    assert q["code"][0]["file"] == "main.py"  # 코드 보고: 새 문제 저장소에서 읽음
    res = client.post(f"/api/drills/runs/{run['run_id']}/cards/1/answer",
                      json={"answers": {"s1": "200", "s2": "예"}}).json()
    assert res["all_correct"] and res["snippets"]

    # 같은 저장소로 카드 더 만들기
    r2 = client.post("/api/drills/decks", json={"repo_id": body["repo_id"], "count": 3})
    assert r2.status_code == 200, r2.text
    assert client.post("/api/drills/decks", json={"repo_id": "f_20990101_000000_abcd"}).status_code == 404
    assert client.post("/api/drills/decks", json={}).status_code == 400
