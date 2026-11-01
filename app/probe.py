"""추적 카드 검증용 probe 실행 (docs/drill_design.md §6.2 G5, DR-5).

probe는 상황을 주입해 코드를 실제로 실행하고, 질문 칸마다 observe(칸 ID, 값)으로 관찰값을 남기는 pytest 파일이다.
정답은 LLM의 주장이 아니라 이 관찰값이다.

- 저장소를 임시 디렉터리에 복사해 probe마다 독립 실행한다 (원본 불변, probe끼리 상태 공유 없음).
- API 키를 가짜 값으로 바꾸고 base URL을 닫힌 포트로 돌려, 실수로 외부 모델을 부르면 바로 실패하게 한다.
"""
import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROBE_DIR = "_drill_probes"
TIMEOUT_SEC = 180  # 첫 실행은 의존성 설치가 끼어 느리다
COPY_SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

PRELUDE = '''# --- drill probe prelude (자동 삽입) ---
import json as _drill_json, os as _drill_os
_DRILL_OBS = {}
def observe(key, value):
    _DRILL_OBS[str(key)] = value
    with open(_drill_os.environ["DRILL_OBS"], "w", encoding="utf-8") as _f:
        _drill_json.dump(_DRILL_OBS, _f, ensure_ascii=False, default=str)
# --- end prelude ---
'''


class ProbeUnsupported(RuntimeError):
    pass


def command(root: Path) -> list[str]:
    """Python 저장소만 지원한다. 의존성 선언 방식에 맞춰 uv 실행 명령을 고른다."""
    base = ["--with", "pytest", "--with", "httpx", "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--rootdir", ".", PROBE_DIR]
    if (root / "requirements.txt").exists():
        return ["uv", "run", "--no-project", "--with-requirements", "requirements.txt", *base]
    if (root / "pyproject.toml").exists():
        return ["uv", "run", *base]
    if not (root / "package.json").exists() and (any(root.glob("*.py")) or any(root.glob("*/*.py"))):
        return ["uv", "run", "--no-project", *base]
    raise ProbeUnsupported("Python 서버 저장소가 아닙니다 (requirements.txt·pyproject.toml 없음, Node 저장소 등).")


def supported(root: Path) -> bool:
    try:
        command(root)
        return True
    except ProbeUnsupported:
        return False


def _env(obs_file: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MOCK_", "OPENAI_", "ANTHROPIC_")) and k not in ("VIRTUAL_ENV", "PYTHONPATH")}
    env.update(OPENAI_API_KEY="sk-drill-probe-not-a-real-key", OPENAI_BASE_URL="http://127.0.0.1:9/v1",
               PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1", DRILL_OBS=str(obs_file))
    return env


def _tail(text: str, n: int = 60) -> str:
    return "\n".join(text.splitlines()[-n:])


def run_one(root: Path, name: str, code: str) -> dict:
    """probe 하나를 저장소 복사본에서 실행한다. 반환: passed, observed, output."""
    root = Path(root)
    cmd = command(root)
    with tempfile.TemporaryDirectory(prefix="drill_probe_") as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(root, work, ignore=shutil.ignore_patterns(*COPY_SKIP), symlinks=True)
        pdir = work / PROBE_DIR
        pdir.mkdir(exist_ok=True)
        (pdir / f"test_{name}.py").write_text(PRELUDE + "\n" + code, encoding="utf-8")
        obs_file = Path(tmp) / "observed.json"
        try:
            r = subprocess.run(cmd, cwd=work, env=_env(obs_file), capture_output=True, text=True,
                               timeout=TIMEOUT_SEC)
            out, code_ = r.stdout + r.stderr, r.returncode
        except subprocess.TimeoutExpired as e:
            out, code_ = f"{e.stdout or ''}{e.stderr or ''}\n{TIMEOUT_SEC}초 시간 초과", None
        observed = {}
        if obs_file.exists():
            try:
                observed = json.loads(obs_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                observed = {}
    return {"passed": code_ == 0, "exit_code": code_, "observed": observed, "output": _tail(out)}


def run_many(root: Path, probes: dict[str, str], workers: int = 4) -> dict[str, dict]:
    if not probes:
        return {}
    # 의존성 캐시를 채우려고 첫 probe는 혼자 돌리고 나머지를 병렬로 돌린다
    names = list(probes)
    results = {names[0]: run_one(root, names[0], probes[names[0]])}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, res in zip(names[1:], pool.map(lambda n: run_one(root, n, probes[n]), names[1:])):
            results[n] = res
    return results
