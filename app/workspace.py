"""작업공간 생성기 (5.2). 앱·봉인 정보에 대한 경로나 힌트를 작업공간에 넣지 않는다."""
import subprocess
from pathlib import Path

from .config import settings
from .schemas import ProblemPublic

ENV_EXAMPLE = "OPENAI_API_KEY=\nOPENAI_BASE_URL=\n"
GITIGNORE = ".env\n.venv/\n__pycache__/\nnode_modules/\n*.pyc\n"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def create(session_id: str, problem: ProblemPublic) -> Path:
    ws = settings.workspace_root / session_id
    ws.mkdir(parents=True)
    (ws / "README.md").write_text(problem.readme_markdown.rstrip() + "\n", encoding="utf-8")
    for f in problem.data_files:
        target = (ws / f.path).resolve()
        if not target.is_relative_to(ws / "data"):
            raise ValueError(f"작업공간 밖 경로: {f.path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")
    (ws / ".env.example").write_text(ENV_EXAMPLE, encoding="utf-8")
    (ws / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "initial: problem files")
    return ws
