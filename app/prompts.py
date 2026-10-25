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


def load(name: str, **vars) -> str:
    return render((PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8"), **vars)
