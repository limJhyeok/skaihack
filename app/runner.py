"""기술 축 자동 실행 (5.9-1, 5.9-2): 스냅샷을 임시 디렉터리에 풀어 실행 명령으로 띄우고 루트 페이지를 확인, 테스트 실행."""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import codectx

START_TIMEOUT = 60
TEST_TIMEOUT = 180
LOG_TAIL = 100


class PortBusy(RuntimeError):
    pass


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _lan_ip() -> str | None:
    """루프백이 아닌 이 기기의 IP. UDP connect는 패킷을 보내지 않고 경로만 정한다."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def _bound_all_interfaces(port: int) -> bool | None:
    """실전 응시 환경의 브라우저 미리보기는 0.0.0.0 바인딩이어야 접근된다. 확인할 IP가 없으면 None."""
    ip = _lan_ip()
    return None if ip is None else _port_open(port, ip)


def _mask(text: str) -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        text = text.replace(key, "***")
    return re.sub(r"sk-[A-Za-z0-9_\-]{16,}", "sk-***", text)


def _env() -> dict:
    """실행 환경: 현재 환경(OPENAI_API_KEY/OPENAI_BASE_URL 포함)에서 이 앱 전용 변수만 뺀다."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MOCK_") and k != "VIRTUAL_ENV"}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _tail(path: Path, n: int) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return _mask("\n".join(lines[-n:]))


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _screenshot(url: str, out: Path) -> str | None:
    try:
        from playwright.sync_api import sync_playwright  # 선택 의존성
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page()
            page.goto(url, timeout=15000)
            page.screenshot(path=str(out), full_page=True)
            b.close()
        return str(out)
    except Exception:  # noqa: BLE001
        return None


def run_app(snapshot_tar: Path, run_command: str | None, port: int | None, out_dir: Path) -> dict:
    """스냅샷을 새로 풀어 실행하고 루트 페이지를 확인한다. 실행 디렉터리는 끝나면 지운다.
    실행 명령·포트는 제출 때 README에서 읽은 값이며, 못 찾았으면 실행하지 않고 실패로 기록한다."""
    if run_command and port and _port_open(port):
        raise PortBusy(f"포트 {port}가 이미 사용 중입니다. 실행 중인 서버를 끄고 다시 평가하세요.")
    work = Path(tempfile.mkdtemp(prefix="mockrun_"))
    app_dir = codectx.extract_snapshot(snapshot_tar, work / "app")
    log_path = out_dir / "run.log"
    result: dict = {"command": run_command, "port": port, "started": False, "exit_code": None,
                    "startup_sec": None, "http_status": None, "content_type": None, "body_head": None,
                    "error": None, "screenshot": None, "bound_all_interfaces": None}
    if not run_command or not port:
        missing = " · ".join(n for n, v in (("실행 명령", run_command), ("포트", port)) if not v)
        result["error"] = f"README에서 {missing}을(를) 찾지 못함 (실전은 제출 때 따로 입력하지 않음)"
        work = Path(tempfile.mkdtemp(prefix="mockrun_"))
        result["log_tail"] = ""
        result["tests"] = run_tests(codectx.extract_snapshot(snapshot_tar, work / "app"))
        shutil.rmtree(work, ignore_errors=True)
        return result
    t0 = time.monotonic()
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(run_command, shell=True, cwd=app_dir, env=_env(), stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while time.monotonic() - t0 < START_TIMEOUT:
                if proc.poll() is not None:
                    result["exit_code"] = proc.returncode
                    result["error"] = f"프로세스가 포트를 열기 전에 종료됨 (exit {proc.returncode})"
                    break
                if _port_open(port):
                    result["started"] = True
                    result["startup_sec"] = round(time.monotonic() - t0, 1)
                    result["bound_all_interfaces"] = _bound_all_interfaces(port)
                    break
                time.sleep(0.5)
            else:
                result["error"] = f"{START_TIMEOUT}초 안에 포트 {port}가 열리지 않음"
            if result["started"]:
                url = f"http://127.0.0.1:{port}/"
                try:
                    with urllib.request.urlopen(url, timeout=10) as r:
                        result["http_status"] = r.status
                        result["content_type"] = r.headers.get("content-type")
                        result["body_head"] = _mask(r.read(2000).decode("utf-8", "replace"))
                except urllib.error.HTTPError as e:
                    result["http_status"] = e.code
                    result["body_head"] = _mask(e.read(1000).decode("utf-8", "replace"))
                except Exception as e:  # noqa: BLE001
                    result["error"] = f"루트 페이지 요청 실패: {e}"
                result["screenshot"] = _screenshot(url, out_dir / "screenshot.png")
        finally:
            _kill(proc)
            for _ in range(20):  # 자식 프로세스까지 내려가 포트가 닫힐 때까지 최대 5초
                if not _port_open(port):
                    break
                time.sleep(0.25)
    result["log_tail"] = _tail(log_path, LOG_TAIL)
    result["tests"] = run_tests(app_dir)
    shutil.rmtree(work, ignore_errors=True)
    return result


def _has_python_tests(root: Path) -> bool:
    return any(p for p in root.rglob("*.py")
               if (p.name.startswith("test_") or p.name.endswith("_test.py")) and ".venv" not in p.parts)


def run_tests(app_dir: Path) -> dict:
    cmd = None
    if _has_python_tests(app_dir):
        cmd = "uv run --with pytest python -m pytest -q" if (app_dir / "pyproject.toml").exists() \
            else "python3 -m pytest -q"
    elif (app_dir / "package.json").exists():
        scripts = json.loads((app_dir / "package.json").read_text() or "{}").get("scripts", {})
        if "test" in scripts and "no test specified" not in scripts["test"]:
            cmd = "npm test --silent"
    if cmd is None:
        return {"found": False, "command": None, "exit_code": None, "output_tail": None}
    try:
        r = subprocess.run(cmd, shell=True, cwd=app_dir, env=_env(), capture_output=True, text=True,
                           timeout=TEST_TIMEOUT)
        out, code = r.stdout + r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        out, code = f"{e.stdout or ''}\n{TEST_TIMEOUT}초 시간 초과", None
    return {"found": True, "command": cmd, "exit_code": code,
            "output_tail": _mask("\n".join(str(out).splitlines()[-60:]))}
