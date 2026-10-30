"""제출과 동결 (5.7): 자동 커밋(선택) → git tag → 스냅샷 → 비밀키 검사 → 기록 수집."""
import json
import re
import subprocess
import tarfile
from datetime import datetime
from pathlib import Path

from . import collector, sessions, timer
from .schemas import Session

EXCLUDE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}

SECRET_PATTERNS = [
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("api_key_assignment", re.compile(r"""OPENAI_API_KEY\s*[=:]\s*["']?(?!\s*$)([^\s"'#]+)""")),
]
# 환경변수에서 읽는 코드는 정상: OPENAI_API_KEY=os.environ[...] 같은 줄은 제외
ENV_READ = re.compile(r"environ|getenv|process\.env|\$\{?OPENAI_API_KEY")
# 문서 속 자리표시자·테스트용 가짜 값 (OPENAI_API_KEY=your-key, =<키>, =..., 'test-key', 'secret')
PLACEHOLDER = re.compile(r"^(\.\.\.|<|your|xxx|changeme|\$|`|test|dummy|fake|secret|example|sample|mock|placeholder)", re.I)


def _git(ws: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], cwd=ws, capture_output=True, text=True, check=check).stdout


def dirty_files(ws: Path) -> list[str]:
    return [line[3:] for line in _git(ws, "status", "--porcelain").splitlines() if line.strip()]


RUN_LINE = re.compile(r"^(uv run |python3? |uvicorn |flask |streamlit |npm (run|start)|node |pnpm |yarn )")
# 서버를 띄우지 않는 명령 (테스트·검사·빌드·설치)
NOT_SERVER = re.compile(r"\b(test|check|lint|build|install|format|typecheck|pytest|ci)\b")
# 우선순위: 포트를 명시한 서버 > start/dev/serve 류 > 그 밖의 실행 명령
SERVER_HINT = re.compile(r"\b(start|dev|serve|server|app|main)\b")


PORT_IN_CMD = re.compile(r"(?:--port[ =]|-p[ =]?|:)(\d{4,5})\b")
PORT_IN_TEXT = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{4,5})\b|(?:포트|port)\s*[:=]?\s*`?(\d{4,5})\b", re.I)


def readme_command(ws: Path) -> tuple[str | None, int | None]:
    """작업공간 README에서 실행 명령과 포트를 찾는다. 실전처럼 제출 때 따로 입력받지 않으므로 기본값을 채우지 않는다."""
    readme = ws / "README.md"
    text = readme.read_text(encoding="utf-8", errors="replace") if readme.exists() else ""
    best, best_rank = None, -1
    for line in text.splitlines():
        s = line.strip().strip("`").removeprefix("$ ").strip()
        if not RUN_LINE.match(s) or NOT_SERVER.search(s):
            continue
        rank = 2 if ("uvicorn" in s or "streamlit" in s or "--port" in s) else 1 if SERVER_HINT.search(s) else 0
        if rank > best_rank:  # 같은 순위면 먼저 나온 명령
            best, best_rank = s, rank
    m = (best and PORT_IN_CMD.search(best)) or PORT_IN_TEXT.search(text)
    port = next((int(g) for g in m.groups() if g), None) if m else None
    return best, port


def _excluded(rel: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    name = rel.name
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def snapshot(ws: Path, target: Path) -> list[str]:
    files = []
    with tarfile.open(target, "w:gz") as tar:
        for p in sorted(ws.rglob("*")):
            rel = p.relative_to(ws)
            if p.is_file() and not p.is_symlink() and not _excluded(rel):
                tar.add(p, arcname=str(rel))
                files.append(str(rel))
    return files


def _mask(s: str) -> str:
    return s[:6] + "…(가림)" if len(s) > 6 else "…(가림)"


def scan_secrets(ws: Path, files: list[str]) -> list[dict]:
    findings = []
    for rel in files:
        p = ws / rel
        if p.stat().st_size > 2_000_000:
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(lines, 1):
            for kind, pat in SECRET_PATTERNS:
                m = pat.search(line)
                if not m:
                    continue
                if kind == "api_key_assignment" and (ENV_READ.search(line) or PLACEHOLDER.match(m.group(1))):
                    continue
                findings.append({"file": rel, "line": i, "kind": kind, "preview": _mask(m.group(0))})
    # .env가 git에 커밋되었는지
    tracked = _git(ws, "ls-files", "--", ".env", "*/.env", check=False).split()
    for t in tracked:
        findings.append({"file": t, "line": None, "kind": "env_file_committed", "preview": ".env가 git에 커밋됨"})
    return findings


def submit(session: Session, agent_tool: str, auto_commit: bool, manual_tokens: int | None) -> dict:
    ws = Path(session.workspace_path)
    d = sessions.session_dir(session.session_id)
    dirty = dirty_files(ws)
    committed = False
    if dirty and auto_commit:
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "-m", "submission snapshot")
        committed = True
    _git(ws, "tag", "-f", "submission")
    run_command, app_port = readme_command(ws)  # 커밋 뒤 README 기준 (실전은 실행 명령·포트를 입력받지 않음)
    files = snapshot(ws, d / "snapshot.tar.gz")
    secrets = scan_secrets(ws, files)

    now = datetime.now().astimezone()
    process = collector.collect(session, now, manual_tokens)
    (d / "process.json").write_text(json.dumps(process, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {
        "submitted_at": now.isoformat(timespec="seconds"),
        "dirty_files_at_submit": dirty,
        "auto_committed": committed,
        "tag": "submission",
        "run_command": run_command,
        "app_port": app_port,
        "snapshot_files": files,
        "secret_findings": secrets,
        "commit_count": len(process["commits"]),
        "agent_status": process["agent_status"],
        "agent_session_count": process["agent_session_count"],
        "token_usage": process["token_usage"],
        "token_source": process["token_source"],
        "docs_found": sorted(process["docs"]),
    }
    (d / "submission.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    session.run_command, session.app_port, session.agent_tool = run_command, app_port, agent_tool
    session.status, session.submitted_at = "submitted", result["submitted_at"]
    sessions.save_session(session)
    timer.log_event(session, "submit", auto_committed=committed, secret_findings=len(secrets))
    return result
