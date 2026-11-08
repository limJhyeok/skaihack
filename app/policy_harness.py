"""정책 묶음 실행 하네스 (docs/flow_drills_design.md 3절).

이 파일은 앱 패키지를 import하지 않는다. 앱은 값 검사(parse·canon)에 import해 쓰고,
실행할 때는 이 파일을 임시 디렉터리에 복사해 `python -I policy_harness.py impl.py`로 돌린다
(정답 구현·결함 버전은 LLM이 쓴 코드라 앱 프로세스 밖에서 실행한다).

stdin: {"cases": [{"id", "func", "params": [{"name", "type", "options", "raw"}]}]}
stdout: [{"id", "ok", "value" | "error"}]  — value는 canon()으로 정규화한 문자열
"""
import importlib.util
import json
import re
import signal
import sys
from datetime import date, datetime, time

TYPES = ("int", "number", "str", "bool", "date", "time", "datetime", "choice", "list")
CASE_TIMEOUT_SEC = 2
TRUE = {"true", "참", "예", "yes", "y", "1", "o"}
FALSE = {"false", "거짓", "아니오", "no", "n", "0", "x"}


def parse(typ: str, raw, options=()) -> object:
    """사용자·출제자가 적은 문자열을 파이썬 값으로. 형식이 틀리면 ValueError(한국어 메시지)."""
    s = str(raw if raw is not None else "").strip()
    try:
        if typ == "int":
            return int(s.replace(",", ""))
        if typ == "number":
            v = float(s.replace(",", ""))
            return int(v) if v.is_integer() else v
        if typ == "str":
            return s
        if typ == "bool":
            if s.lower() in TRUE:
                return True
            if s.lower() in FALSE:
                return False
            raise ValueError
        if typ == "date":
            return date.fromisoformat(s)
        if typ == "time":
            return time.fromisoformat(s if len(s) != 4 else "0" + s)
        if typ == "datetime":
            return datetime.fromisoformat(s.replace("T", " "))
        if typ == "choice":
            if s not in options:
                raise ValueError
            return s
        if typ == "list":
            return [x.strip() for x in s.split(",") if x.strip()]
    except ValueError:
        pass
    hint = {"int": "정수", "number": "숫자", "bool": "true/false", "date": "YYYY-MM-DD", "time": "HH:MM",
            "datetime": "YYYY-MM-DD HH:MM", "choice": "선택지 " + "/".join(options), "list": "쉼표로 구분"}
    if typ not in TYPES:
        raise ValueError(f"알 수 없는 형식: {typ}")
    raise ValueError(f"'{s}'는 {hint.get(typ, typ)} 형식이 아님")


def canon(v) -> str:
    """비교용 정규화. 파이썬 타입 기준이라 반환 타입 선언과 조금 달라도 같은 값이면 같게 나온다."""
    if v is None:
        return "없음"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return str(v) if isinstance(v, int) else f"{v:.6g}"
    if isinstance(v, (list, tuple)):
        return ", ".join(canon(x) for x in v)
    if hasattr(v, "value") and not isinstance(v, str):  # Enum
        return canon(v.value)
    return re.sub(r"\s+", " ", str(v)).strip()


def expected_canon(typ: str, raw, options=()) -> str:
    return canon(parse(typ, raw, options))


def _timeout(signum, frame):
    raise TimeoutError(f"{CASE_TIMEOUT_SEC}초 안에 끝나지 않음")


def main() -> None:
    req = json.loads(sys.stdin.read())
    out = []
    try:
        spec = importlib.util.spec_from_file_location("policy_impl", sys.argv[1])
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001 — 모듈 자체가 깨진 결함 버전
        err = f"모듈 로드 실패: {type(e).__name__}: {e}"
        print(json.dumps([{"id": c["id"], "ok": False, "error": err} for c in req["cases"]], ensure_ascii=False))
        return
    signal.signal(signal.SIGALRM, _timeout)
    for c in req["cases"]:
        try:
            fn = getattr(mod, c["func"])
            kwargs = {p["name"]: parse(p["type"], p["raw"], p.get("options") or ()) for p in c["params"]}
            signal.alarm(CASE_TIMEOUT_SEC)
            try:
                value = fn(**kwargs)
            finally:
                signal.alarm(0)
            out.append({"id": c["id"], "ok": True, "value": canon(value)})
        except Exception as e:  # noqa: BLE001
            out.append({"id": c["id"], "ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
