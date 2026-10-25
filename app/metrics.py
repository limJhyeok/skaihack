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


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def readme_overlap(readme: str, prompt: str) -> float:
    grams = _ngrams(_norm(readme))
    return len(grams & _ngrams(_norm(prompt))) / len(grams) if grams else 0.0


def first_impl_commit(commits: list[dict]) -> dict | None:
    """초기 커밋 이후 코드 파일을 처음 건드린 커밋."""
    for c in commits[1:]:
        if any(Path(line.split("|")[0].strip()).suffix in CODE_EXT for line in c.get("stat", "").splitlines()):
            return c
    return None


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
    ids = [f.id for f in sealed.facts if pred(f)]
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


def compute(*, readme: str, sealed: Sealed, chat: list[dict], process: dict, workspace: Path) -> dict:
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
            "partial_only": sum(1 for v in disc.values() if v["partial"] and not v["discovered"]),
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
        "tokens": process.get("token_usage"),
        "token_source": process.get("token_source"),
        "commits": len(commits),
    }
