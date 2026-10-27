"""prompts/*.md 로딩과 {{var}} 치환."""
import re

from .config import APP_ROOT

PROMPT_DIR = APP_ROOT / "prompts"


def render(template: str, **vars) -> str:
    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in vars:
            raise KeyError(f"프롬프트 변수 누락: {key}")
        return str(vars[key])

    return re.sub(r"\{\{\s*(\w+)\s*\}\}", sub, template)


def duration_text(minutes: int) -> str:
    """시험 작업 시간을 프롬프트용 문구로 바꾼다. 180 → '3시간', 150 → '2시간 30분'."""
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m}분"
    return f"{h}시간" + (f" {m}분" if m else "")


def load(name: str, **vars) -> str:
    return render((PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8"), **vars)
