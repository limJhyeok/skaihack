"""과정 기록 수집기 (5.6). 수집 실패는 평가를 막지 않는다: 없으면 "수집 안 됨"으로 남긴다."""
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import agent_logs, sessions
from .config import settings
from .schemas import Session

WORKSPACE_DOCS = ["NOTES.md", "SCOPE.md", "ACCEPTANCE.md", "DECISIONS.md", "RISKS.md", "HANDOFF.md", "AGENTS.md"]
NOT_COLLECTED = "수집 안 됨"


# ---------- 로그 경로 설정 (설정 화면) ----------

def _settings_file() -> Path:
    return settings.data_dir / "settings.json"


def log_paths() -> dict:
    paths = agent_logs.default_paths()
    if _settings_file().exists():
        paths.update({k: v for k, v in json.loads(_settings_file().read_text()).items() if v})
    return paths


def save_log_paths(paths: dict) -> dict:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    keep = {k: (paths.get(k) or "").strip() for k in agent_logs.default_paths()}
    _settings_file().write_text(json.dumps(keep, ensure_ascii=False, indent=2))
    return log_paths()


# ---------- git ----------

def git_commits(workspace: Path, started_at: str | None) -> list[dict]:
    sep = "@@COMMIT@@"
    r = subprocess.run(["git", "log", "--reverse", "--stat", f"--format={sep}%H|%aI|%s"],
                       cwd=workspace, capture_output=True, text=True, check=True)
    start = datetime.fromisoformat(started_at) if started_at else None
    commits = []
    for block in r.stdout.split(sep)[1:]:
        head, _, stat = block.partition("\n")
        h, ts, subject = head.split("|", 2)
        commits.append({
            "hash": h[:10], "ts": ts, "subject": subject, "stat": stat.strip(),
            "elapsed_sec": int((datetime.fromisoformat(ts) - start).total_seconds()) if start else None,
        })
    return commits


# ---------- 수집 ----------

def collect(session: Session, end: datetime, manual_tokens: int | None = None) -> dict:
    ws = Path(session.workspace_path)
    out: dict = {"collected_at": sessions.now_iso()}
    try:
        out["commits"] = git_commits(ws, session.started_at)
    except Exception as e:  # noqa: BLE001
        out["commits"], out["commits_error"] = [], f"{NOT_COLLECTED}: {e}"

    out["docs"] = {name: (ws / name).read_text(encoding="utf-8", errors="replace")
                   for name in WORKSPACE_DOCS if (ws / name).is_file()}

    start = datetime.fromisoformat(session.started_at) if session.started_at else end
    copy_dir = sessions.session_dir(session.session_id) / "agent_logs"
    traces, errors = [], []
    for adapter in agent_logs.adapters(log_paths()):
        try:
            for p in adapter.find_sessions(ws, start, end):
                t = adapter.parse(p)
                t.user_prompts = [x for x in t.user_prompts
                                  if x["ts"] and start <= datetime.fromisoformat(x["ts"].replace("Z", "+00:00")) <= end]
                copy_dir.mkdir(exist_ok=True)
                shutil.copy2(p, copy_dir / f"{adapter.name}_{p.name}")
                traces.append(t.to_dict())
        except Exception as e:  # noqa: BLE001
            errors.append(f"{adapter.name}: {e}")
    traces.sort(key=lambda t: t["started_at"] or "")
    for t in traces:
        t["elapsed_start_sec"] = int((datetime.fromisoformat(t["started_at"].replace("Z", "+00:00")) - start).total_seconds())
        for x in t["user_prompts"]:
            x["elapsed_sec"] = int((datetime.fromisoformat(x["ts"].replace("Z", "+00:00")) - start).total_seconds())
    out["agent_traces"] = traces
    out["agent_status"] = "ok" if traces else NOT_COLLECTED
    out["agent_errors"] = errors
    out["agent_session_count"] = len(traces)
    out["agent_session_starts"] = [t["started_at"] for t in traces]

    usages = [t["token_usage"] for t in traces if t["token_usage"]]
    if usages:
        out["token_usage"] = {k: sum(u[k] for u in usages) for k in usages[0]}
        out["token_source"] = "agent_logs"
    elif manual_tokens:
        out["token_usage"] = {"total": manual_tokens, "billable": manual_tokens}
        out["token_source"] = "manual"
    else:
        out["token_usage"], out["token_source"] = None, NOT_COLLECTED
    return out
