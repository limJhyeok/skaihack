"""의도 관리 축 정량 지표 (5.9). 모두 코드로 계산하고 LLM 평가의 입력·근거로 쓴다."""
import re
import statistics
import subprocess
from datetime import datetime
from pathlib import Path

from .codectx import CODE_EXT
from .generator import _ngrams, _norm
from .schemas import Sealed

PASTE_THRESHOLD = 0.7
PLAN_RE = re.compile(r"계획|설계|대안|방안|선택지|옵션|트레이드오프|장단점|구조를? (제안|잡)|어떻게 (구현|만들)|plan|design|option|trade-?off|approach", re.I)
VERIFY_RE = re.compile(r"테스트|검증|확인해|확인하|실행해|실행하|돌려|리뷰|교차|재현|curl|pytest|에러 ?로그|로그를? 봐|test|verify|review|check", re.I)
REGEN_RE = re.compile(r"처음부터|전부 다시|전체를? 다시|다시 (만들|작성|짜)|새로 (만들|작성|짜)|갈아엎|싹 ?다|from scratch|rewrite|start over", re.I)
SPIKE_MIN = 20_000  # 이 이하의 증가는 급증으로 보지 않음
SPIKE_FACTOR = 3
# 명령을 &&·||·;·| 로 나눈 각 조각의 "실행 파일" 자리에서만 찾는다 (grep "pytest" 같은 검색어는 제외)
CMD_SPLIT_RE = re.compile(r"&&|\|\||;|\||\n")
RUNNER_PREFIX = r"^(?:(?:timeout\s+\S+|time|env|\w+=\S*|uv run(?:\s+--\S+)*|poetry run|npx|pnpm exec|python3?\s+-m)\s+)*"
TEST_SEG_RE = re.compile(RUNNER_PREFIX + r"(?:pytest|py\.test|unittest|vitest|jest|mocha|playwright test|go test|cargo test"
                         r"|(?:npm|pnpm|yarn|bun)(?: run)? test|(?:python3?\s+)?manage\.py test)\b", re.I)
SMOKE_SEG_RE = re.compile(RUNNER_PREFIX + r"(?:curl|wget|https?)\b.*(?:localhost|127\.0\.0\.1|0\.0\.0\.0)", re.I)
# 테스트 러너 요약 줄. 실패 표시를 통과보다 먼저 본다
TEST_FAIL_OUT_RE = re.compile(r"\b\d+ (?:failed|failing|errors?)\b|^FAILED\b|^FAIL\b|Tests:.*\bfailed", re.I | re.M)
TEST_PASS_OUT_RE = re.compile(r"\b\d+ (?:passed|passing)\b|^ok\s|^OK\b|Tests:.*\bpassed", re.M)
TIMELINE_CAP = 40
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock", "Cargo.lock"}
STAT_LINE_RE = re.compile(r"^\s*(.+?)\s+\|\s+(\d+)")
BIG_BANG_MIN_LINES = 150
BIG_BANG_SHARE = 0.6
FINAL_WINDOW_SEC = 30 * 60


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def readme_overlap(readme: str, prompt: str) -> float:
    grams = _ngrams(_norm(readme))
    return len(grams & _ngrams(_norm(prompt))) / len(grams) if grams else 0.0


def _touches_code(c: dict) -> bool:
    return any(Path(line.split("|")[0].strip()).suffix in CODE_EXT for line in c.get("stat", "").splitlines())


def first_impl_commit(commits: list[dict]) -> dict | None:
    """초기 커밋 이후 코드 파일을 처음 건드린 커밋."""
    return next((c for c in commits[1:] if _touches_code(c)), None)


def command_kind(cmd: str) -> str | None:
    segs = [s.strip() for s in CMD_SPLIT_RE.split(cmd)]
    if any(TEST_SEG_RE.match(s) for s in segs):
        return "test"
    if any(SMOKE_SEG_RE.match(s) for s in segs):
        return "smoke"
    return None


def test_outcome(c: dict) -> bool | None:
    """테스트 요약 줄이 출력에 있으면 그것으로, 없으면 종료 상태로 판정."""
    out = c.get("output_tail") or ""
    if TEST_FAIL_OUT_RE.search(out):
        return False
    if TEST_PASS_OUT_RE.search(out):
        return True
    return c.get("ok")


def verification_runs(traces: list[dict], commits: list[dict]) -> dict:
    """에이전트가 실제로 실행한 테스트·서버 확인 명령의 타임라인과 결과."""
    runs = []
    for si, t in enumerate(traces, 1):
        for c in t.get("commands", []):
            kind = command_kind(c["cmd"])
            if kind:
                runs.append({"session": si, "elapsed_sec": c.get("elapsed_sec"), "kind": kind, "cmd": c["cmd"][:120],
                             "ok": test_outcome(c) if kind == "test" else c.get("ok")})
    runs.sort(key=lambda r: r["elapsed_sec"] if r["elapsed_sec"] is not None else -1)
    tests = [r for r in runs if r["kind"] == "test"]
    passed = [r for r in tests if r["ok"] is True]
    first_fail = next((r for r in tests if r["ok"] is False), None)
    last_pass = passed[-1] if passed else None
    after_pass = None
    if last_pass and last_pass["elapsed_sec"] is not None:
        after_pass = sum(1 for c in commits[1:] if _touches_code(c)
                         and (c.get("elapsed_sec") or 0) > last_pass["elapsed_sec"])
    return {
        "test_runs": len(tests),
        "test_passed": len(passed),
        "test_failed": sum(r["ok"] is False for r in tests),
        "first_test_elapsed_sec": tests[0]["elapsed_sec"] if tests else None,
        "last_test": {"elapsed_sec": tests[-1]["elapsed_sec"], "ok": tests[-1]["ok"]} if tests else None,
        "fail_then_pass": bool(first_fail and any((r["elapsed_sec"] or 0) > (first_fail["elapsed_sec"] or 0) for r in passed)),
        # 1이면 보통 "테스트 → 커밋" 순서. 2 이상이면 마지막 통과 뒤에 검증 없이 코드가 더 바뀐 것
        "code_commits_after_last_pass": after_pass,
        "smoke_checks": sum(r["kind"] == "smoke" for r in runs),
        "timeline": runs[:TIMELINE_CAP],
    }


def commit_sizes(commits: list[dict], duration_sec: int | None) -> dict:
    """초기 커밋 제외, 커밋별 변경 줄 수(lock 파일·node_modules 제외)의 분포."""
    rows = []
    for c in commits[1:]:
        lines = 0
        for line in c.get("stat", "").splitlines():
            m = STAT_LINE_RE.match(line)
            if m and Path(m.group(1)).name not in LOCK_FILES and "node_modules" not in m.group(1):
                lines += int(m.group(2))
        rows.append({"hash": c["hash"], "elapsed_sec": c.get("elapsed_sec"), "lines": lines})
    total = sum(r["lines"] for r in rows)
    if not total:
        return {"commits": len(rows), "total_lines": 0, "median_lines": None, "max_lines": None, "max_commit": None,
                "max_share": None, "final_30min_share": None, "big_bang": False}
    big = max(rows, key=lambda r: r["lines"])
    final = None
    if duration_sec:
        final = round(sum(r["lines"] for r in rows if (r["elapsed_sec"] or 0) >= duration_sec - FINAL_WINDOW_SEC) / total, 2)
    share = round(big["lines"] / total, 2)
    return {"commits": len(rows), "total_lines": total, "median_lines": statistics.median(r["lines"] for r in rows),
            "max_lines": big["lines"], "max_commit": big["hash"], "max_share": share, "final_30min_share": final,
            "big_bang": total >= BIG_BANG_MIN_LINES and share >= BIG_BANG_SHARE}


def file_added_at(workspace: Path, name: str) -> str | None:
    try:
        out = subprocess.run(["git", "log", "--diff-filter=A", "--format=%aI", "--", name], cwd=workspace,
                             capture_output=True, text=True, check=True).stdout.split()
        return out[-1] if out else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def file_commit_times(workspace: Path, name: str) -> list[str]:
    try:
        return subprocess.run(["git", "log", "--format=%aI", "--", name], cwd=workspace,
                              capture_output=True, text=True, check=True).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def discovery(sealed: Sealed, chat: list[dict]) -> dict[str, dict]:
    """사실별 발견 여부와 발견한 질문 번호(사용자 질문 기준 1부터)."""
    out = {f.id: {"discovered": False, "partial": False, "question_no": None, "chat_index": None} for f in sealed.facts}
    qno = 0
    for i, r in enumerate(chat):
        if r["role"] == "user":
            qno += 1
            continue
        for fid in r.get("disclosed_fact_ids") or []:
            if fid in out and not out[fid]["discovered"]:
                out[fid].update(discovered=True, question_no=qno, chat_index=i)
        for fid in r.get("partial_fact_ids") or []:
            if fid in out:
                out[fid]["partial"] = True
    return out


def _rate(sealed: Sealed, disc: dict, pred) -> dict:
    """대화 발견률. README에 묻힌 사실(buried)은 대화 발견 대상이 아니라 빼고 센다."""
    ids = [f.id for f in sealed.facts if f.type != "buried" and pred(f)]
    found = sum(disc[i]["discovered"] for i in ids)
    return {"found": found, "total": len(ids), "rate": round(found / len(ids), 2) if ids else None}


def token_spikes(traces: list[dict]) -> list[dict]:
    """프롬프트별 토큰 증가량이 중앙값의 3배 이상이고 2만 이상인 구간."""
    rows = []
    for si, t in enumerate(traces, 1):
        timeline = [(_ts(x["ts"]), x["billable"]) for x in t.get("token_timeline", []) if x.get("ts")]
        prompts = [(_ts(p["ts"]), p) for p in t["user_prompts"] if p.get("ts")]
        for k, (pts, p) in enumerate(prompts):
            end = prompts[k + 1][0] if k + 1 < len(prompts) else None
            before = max([b for ts, b in timeline if ts <= pts], default=0)
            after = max([b for ts, b in timeline if end is None or ts < end], default=before)
            rows.append({"session": si, "elapsed_sec": p.get("elapsed_sec"), "prompt": p["text"][:200],
                         "tokens": max(0, after - before)})
    if len(rows) < 2:
        return []
    med = statistics.median(r["tokens"] for r in rows) or 1
    return [r for r in rows if r["tokens"] >= SPIKE_MIN and r["tokens"] >= SPIKE_FACTOR * med]


def compute(*, readme: str, sealed: Sealed, chat: list[dict], process: dict, workspace: Path,
            duration_sec: int | None = None) -> dict:
    commits = process.get("commits", [])
    traces = process.get("agent_traces", [])
    docs = process.get("docs", {})
    first_impl = first_impl_commit(commits)
    first_impl_ts = _ts(first_impl["ts"]) if first_impl else None

    # ---- 담당자 인터뷰
    users = [r for r in chat if r["role"] == "user"]
    personas = [r for r in chat if r["role"] == "persona" and r.get("question_quality")]
    specs = [r["question_quality"]["specificity"] for r in personas]
    disc = discovery(sealed, chat)
    agree = next((r for r in personas if r["question_quality"].get("scope_agreed")), None)
    proposals = sum(bool(r["question_quality"].get("is_scope_proposal")) for r in personas)

    # ---- 에이전트 프롬프트
    prompts = [dict(p, session=si) for si, t in enumerate(traces, 1) for p in t["user_prompts"]]
    pasted = [p for p in prompts if readme_overlap(readme, p["text"]) >= PASTE_THRESHOLD]
    regen = [p for p in prompts if REGEN_RE.search(p["text"])]
    verify = [p for p in prompts if VERIFY_RE.search(p["text"])]
    before_impl = [p for p in prompts if first_impl_ts is None or _ts(p["ts"]) < first_impl_ts]
    planning = [p for p in before_impl if PLAN_RE.search(p["text"])]

    # ---- 문서·세션 전환
    acc_added = file_added_at(workspace, "ACCEPTANCE.md")
    handoff_times = [_ts(x) for x in file_commit_times(workspace, "HANDOFF.md")]
    switches = []
    for i in range(1, len(traces)):
        prev_start, start = _ts(traces[i - 1]["started_at"]), _ts(traces[i]["started_at"])
        committed = any(prev_start <= h <= start for h in handoff_times if h)
        edited = any(Path(f).name == "HANDOFF.md" for f in traces[i - 1].get("files_edited", []))
        switches.append({"to_session": i + 1, "started_at": traces[i]["started_at"],
                         "handoff_committed_before": committed, "handoff_edited_in_prev_session": edited})

    def brief(p):
        return {"session": p["session"], "elapsed_sec": p.get("elapsed_sec"), "text": p["text"][:200]}

    return {
        "interview": {
            "questions": len(users),
            "avg_specificity": round(statistics.mean(specs), 2) if specs else None,
            "dump_requests": sum(bool(r["question_quality"].get("is_dump_request")) for r in personas),
            "confirmations": sum(bool(r["question_quality"].get("is_confirmation")) for r in personas),
            "discovery": {
                "all": _rate(sealed, disc, lambda f: True),
                "critical": _rate(sealed, disc, lambda f: f.importance == "critical"),
                "constraint": _rate(sealed, disc, lambda f: f.type == "constraint"),
                "conflict": _rate(sealed, disc, lambda f: f.type == "conflict"),
            },
            "partial_only": sum(1 for f in sealed.facts if f.type != "buried"
                                and disc[f.id]["partial"] and not disc[f.id]["discovered"]),
            "questions_before_first_impl_commit": sum(1 for r in users if first_impl_ts is None or _ts(r["ts"]) < first_impl_ts),
            "scope_proposals": proposals,
            "written_agreement": {"exists": agree is not None, "elapsed_sec": agree["elapsed_sec"] if agree else None},
        },
        "acceptance": {
            "exists": "ACCEPTANCE.md" in docs,
            "first_committed_at": acc_added,
            "before_first_impl_commit": bool(acc_added and first_impl_ts and _ts(acc_added) <= first_impl_ts)
            if acc_added else False,
        },
        "first_impl_commit": {"hash": first_impl["hash"], "elapsed_sec": first_impl["elapsed_sec"]} if first_impl else None,
        "agent": {
            "status": process.get("agent_status"),
            "prompts": len(prompts),
            "avg_prompt_chars": round(statistics.mean(len(p["text"]) for p in prompts)) if prompts else None,
            "readme_paste": [dict(brief(p), overlap=round(readme_overlap(readme, p["text"]), 2)) for p in pasted],
            "planning_before_impl": [brief(p) for p in planning],
            "verification_ratio": round(len(verify) / len(prompts), 2) if prompts else None,
            "verification_prompts": len(verify),
            "full_regeneration": [brief(p) for p in regen],
            "token_spikes": token_spikes(traces),
            "sessions": len(traces),
            "session_switches": switches,
        },
        "verification_runs": verification_runs(traces, commits),
        "commit_sizes": commit_sizes(commits, duration_sec),
        "tokens": process.get("token_usage"),
        "token_source": process.get("token_source"),
        "commits": len(commits),
    }
