"""출제 엔진 (5.1): 도메인·유형·난이도 선택 → LLM 출제 → 코드로 출력 검증 → 실패 시 최대 2회 재생성."""
import random
import re

from . import llm, prompts
from .config import settings
from .schemas import Difficulty, GeneratedProblem

DOMAINS = [
    "이스포츠·게임 운영", "뷰티·커머스", "물류·배송", "병원·클리닉 접수", "학원·교육", "공공 민원",
    "사내 HR·온보딩", "미디어·콘텐츠 제작", "고객센터", "외식·F&B", "여행·숙박", "피트니스",
    "비영리·봉사 단체", "부동산 중개", "도서관·출판",
]

ARCHETYPES = {
    "voice_practice_service": "음성 입력 → 분석 → 피드백 (예: 인터뷰 연습, 발표 연습, 전화 응대 연습)",
    "text_feedback_tool": "사용자가 쓴 글에 기준 기반 피드백 (예: 자기소개서 첨삭, 상담 답변 검수)",
    "document_qa_assistant": "제공된 문서 묶음 기반 질의응답 (예: 사내 규정 도우미, 매장 운영 매뉴얼 도우미)",
    "data_insight_dashboard": "CSV 데이터 분석과 요약 (예: 매출·예약·문의 데이터 인사이트)",
    "workflow_automation": "반복 업무 자동화 (예: 문의 분류·답장 초안, 일정 조율)",
    "classification_triage": "입력을 분류·우선순위화 (예: 민원 분류, 리뷰 감성·이슈 분류)",
    "content_generator_with_rules": "규칙을 지키는 콘텐츠 생성 (예: 브랜드 가이드 준수 문구 생성)",
}

# 각 값은 (최소, 최대). 실제 개수는 이 범위에서 무작위로 고른다.
DIFFICULTY = {
    "easy": {"facts": (6, 8), "conflict": (0, 1), "constraint": (1, 1), "data_files": (0, 1), "traps": (2, 2)},
    "standard": {"facts": (9, 12), "conflict": (1, 2), "constraint": (2, 2), "data_files": (1, 2), "traps": (3, 3)},
    "hard": {"facts": (13, 16), "conflict": (2, 3), "constraint": (3, 3), "data_files": (2, 4), "traps": (4, 4)},
}

README_SECTIONS = ["상황", "현업의 요청", "현업 파트너", "과제", "실행 환경"]
README_LEN = (400, 1200)
MAX_ATTEMPTS = 3  # 최초 1회 + 재생성 2회


class GenerationError(RuntimeError):
    pass


# ---------- 선택 ----------

def pick(domain: str | None, archetype: str | None, difficulty: Difficulty,
         seed: int | None, used_domains: set[str]) -> dict:
    rng = random.Random(seed)
    if not domain:
        fresh = [d for d in DOMAINS if d not in used_domains] or DOMAINS
        domain = rng.choice(fresh)
    if not archetype:
        archetype = rng.choice(list(ARCHETYPES))
    if archetype not in ARCHETYPES:
        raise ValueError(f"알 수 없는 과제 유형: {archetype}")
    p = DIFFICULTY[difficulty]
    counts = {k: rng.randint(*v) for k, v in p.items()}
    return {"domain": domain, "archetype": archetype, "difficulty": difficulty, **counts}


# ---------- 출력 검증 ----------

def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _ngrams(text: str, n: int = 3) -> set[str]:
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def leaks(statement: str, readme: str, extra_texts: list[str] = ()) -> bool:
    """사실 문장이 README(또는 데이터 파일)에 노출되었는지 판정.

    - README와 문자 3-gram 겹침률이 0.6 이상이면 노출
    - README·데이터 파일 어디든 공백·문장부호 제거 후 15자 이상 연속 일치하면 노출
    """
    s = _norm(statement)
    r = _norm(readme)
    grams = _ngrams(s)
    if grams and len(grams & _ngrams(r)) / len(grams) >= 0.6:
        return True
    haystacks = [r] + [_norm(t) for t in extra_texts]
    window = 15
    for i in range(len(s) - window + 1):
        chunk = s[i:i + window]
        if any(chunk in h for h in haystacks):
            return True
    return False


def readme_length(readme: str) -> int:
    return len(readme.strip())


def validate(p: GeneratedProblem) -> list[str]:
    errors: list[str] = []
    headings = [m.strip() for m in re.findall(r"^##\s+(.+)$", p.readme_markdown, flags=re.M)]
    for sec in README_SECTIONS:
        if not any(h.startswith(sec) for h in headings):
            errors.append(f"README에 '## {sec}' 섹션이 없음")
    n = readme_length(p.readme_markdown)
    if not README_LEN[0] <= n <= README_LEN[1]:
        errors.append(f"README 길이 {n}자 (허용 {README_LEN[0]}~{README_LEN[1]}자)")
    if sum(f.importance == "critical" for f in p.facts) < 2:
        errors.append("critical 사실이 2개 미만")
    data_texts = [d.content for d in p.data_files]
    for f in p.facts:
        if not f.reveal_when.strip() or not f.example_question.strip():
            errors.append(f"{f.id}: reveal_when 또는 example_question 누락")
        if leaks(f.statement, p.readme_markdown, data_texts):
            errors.append(f"{f.id}: 사실 내용이 README/데이터에 노출됨")
    for d in p.data_files:
        if not d.path.startswith("data/") or ".." in d.path:
            errors.append(f"데이터 파일 경로가 data/ 아래가 아님: {d.path}")
    return errors


# ---------- 생성 ----------

def generate(choice: dict) -> GeneratedProblem:
    feedback = ""
    errors: list[str] = []
    for _ in range(MAX_ATTEMPTS):
        system = prompts.load(
            "generate_problem",
            domain=choice["domain"],
            archetype=choice["archetype"],
            archetype_description=ARCHETYPES[choice["archetype"]],
            difficulty=choice["difficulty"],
            fact_count=choice["facts"],
            conflict_count=choice["conflict"],
            constraint_count=choice["constraint"],
            data_file_count=choice["data_files"],
            trap_count=choice["traps"],
            example_readme=(prompts.PROMPT_DIR / "example_readme.md").read_text(encoding="utf-8"),
            retry_feedback=feedback,
        )
        problem = llm.parse(
            model=settings.model_strong, system=system,
            user="조건에 맞는 과제 하나를 출제하라.", schema=GeneratedProblem, purpose="generate_problem",
        )
        errors = validate(problem)
        if not errors:
            return problem
        # 실패 사유는 숨은 사실 내용 없이 항목명만 넘긴다.
        feedback = "\n[이전 출제가 검증에 실패한 이유 — 이번에는 반드시 고칠 것]\n" + "\n".join(f"- {e}" for e in errors) + "\n"
    raise GenerationError("출제 검증 실패 (재생성 2회 후): " + "; ".join(errors))
