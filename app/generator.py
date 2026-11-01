"""출제 엔진 (5.1): 도메인·유형·난이도·AI 적합도·기술 조건·숨은 사실 주제 선택 → LLM 출제 → 코드로 출력 검증 → 실패 시 최대 2회 재생성."""
import random
import re

from . import llm, prompts
from .config import settings
from .schemas import Difficulty, GeneratedProblem

DOMAINS = [
    "이스포츠·게임 운영", "뷰티·커머스", "물류·배송", "병원·클리닉 접수", "학원·교육", "공공 민원",
    "사내 HR·온보딩", "미디어·콘텐츠 제작", "고객센터", "외식·F&B", "여행·숙박", "피트니스",
    "비영리·봉사 단체", "부동산 중개", "도서관·출판",
    "사내 총무·시설", "공유오피스·코워킹", "항공·교통", "공연·전시 티켓", "중고거래·마켓",
    "주차·모빌리티", "지자체 체육시설", "택배·퀵 배송", "어린이집·돌봄", "캠핑장·레저",
]

# AI 적합도: 과제에서 LLM(OpenAI API)이 맡는 자리. 응시자에게는 알려 주지 않는다.
AI_FIT = {
    "core": "LLM이 서비스의 핵심 기능을 맡는다. 다만 LLM 결과를 검증·보정하는 장치가 품질을 가른다.",
    "assist": "핵심은 규칙·데이터 기반 업무 시스템이다. LLM은 있어도 되는 보조 기능(요약, 자연어 입력 등) 정도이며 없어도 과제가 성립한다.",
    "unfit": "LLM을 쓰면 오히려 해가 되는 과제다. 결과가 매번 같아야 하거나, 정확한 계산·규칙 적용·근거 설명이 필요하거나, 개인정보를 외부로 보내면 안 되는 등의 이유로 결정적 로직이 정답이다. "
             "README에서 AI 사용을 권하거나 금지하지 않는다. 담당자와의 대화(숨은 사실)에서 LLM이 맞지 않는 이유가 드러나게 하고, "
             "overengineering_traps에 'LLM으로 핵심 판단(배정·계산·추천 등)을 대신하기'를 반드시 넣는다.",
}

# 과제 유형: 설명, 허용되는 AI 적합도, 숨은 사실 주제 후보.
ARCHETYPES = {
    # --- LLM 중심 ---
    "voice_practice_service": {
        "description": "음성 입력 → 분석 → 피드백 (예: 인터뷰 연습, 발표 연습, 전화 응대 연습)",
        "ai_fit": ["core"],
        "themes": ["평가 기준의 우선순위", "녹음·음성 데이터 보관 기한", "점수 대신 서술형 피드백 선호", "연습 대상자의 수준 차이", "피드백 말투"],
    },
    "text_feedback_tool": {
        "description": "사용자가 쓴 글에 기준 기반 피드백 (예: 자기소개서 첨삭, 상담 답변 검수)",
        "ai_fit": ["core"],
        "themes": ["담당자만 아는 판단 기준", "고쳐 쓰기 대신 지적만 원함", "금지 표현 목록", "개인정보 마스킹", "기준 문서의 최신판"],
    },
    "document_qa_assistant": {
        "description": "제공된 문서 묶음 기반 질의응답 (예: 사내 규정 도우미, 매장 운영 매뉴얼 도우미)",
        "ai_fit": ["core"],
        "themes": ["문서 간 충돌 시 우선하는 문서", "근거 조항 표시 필수", "답할 수 없는 질문의 처리", "폐기된 규정", "질문자 권한별 공개 범위"],
    },
    "classification_triage": {
        "description": "입력을 분류·우선순위화 (예: 민원 분류, 리뷰 감성·이슈 분류)",
        "ai_fit": ["core", "assist"],
        "themes": ["분류 체계 개편(옛 분류 vs 새 분류)", "긴급 건 판단 기준", "사람 검토가 필요한 경계 사례", "담당 부서 배정 규칙", "오분류 시 비용"],
    },
    "content_generator_with_rules": {
        "description": "규칙을 지키는 콘텐츠 생성 (예: 브랜드 가이드 준수 문구 생성)",
        "ai_fit": ["core"],
        "themes": ["금지어·법적 표기", "채널별 길이 제한", "가이드 최신 버전", "승인 절차", "톤 예외 상황"],
    },
    # --- 업무 시스템 ---
    "data_insight_dashboard": {
        "description": "CSV 데이터 분석과 요약 (예: 매출·예약·문의 데이터 인사이트)",
        "ai_fit": ["assist", "unfit"],
        "themes": ["지표 정의(무엇을 매출로 셀지)", "제외해야 할 데이터(테스트·취소 건)", "집계 기준 시각·시간대", "이상치 처리", "보는 사람별 관심 지표"],
    },
    "workflow_automation": {
        "description": "반복 업무 자동화 (예: 문의 분류·답장 초안, 일정 조율)",
        "ai_fit": ["core", "assist"],
        "themes": ["자동 처리하면 안 되는 예외", "사람 승인 단계", "발송 시간 제한", "중복 처리 방지", "변경 이력 보관"],
    },
    "reservation_booking": {
        "description": "자원 예약 시스템 (예: 회의실·장비·진료·시설 예약)",
        "ai_fit": ["assist", "unfit"],
        "themes": ["동시 예약·중복 방지", "예약 가능 시간·휴무일", "취소·노쇼 규칙", "우선 예약 권한(직급·부서)", "정원·최소 인원", "반복 예약", "대기자 처리"],
    },
    "inventory_ordering": {
        "description": "재고·입출고·발주 관리 (예: 매장 재고, 비품 관리, 식자재 발주)",
        "ai_fit": ["assist", "unfit"],
        "themes": ["재고가 음수가 되는 경우", "유통기한·선입선출", "발주 기준점과 최소 발주 단위", "단위 환산(박스·낱개)", "실사 재고와 시스템 재고 불일치", "출고 승인 권한"],
    },
    "route_scheduling": {
        "description": "노선·배차·동선 설계와 운영 (예: 출퇴근 셔틀 노선, 배송 경로, 순회 점검 일정)",
        "ai_fit": ["assist", "unfit"],
        "themes": ["정류장·경유지 제약", "탑승 인원·차량 정원", "시간대별 수요 차이", "노선 변경 공지 시점", "도착 시각 보장 기준", "예외 일정(휴일·행사)"],
    },
    "order_payment": {
        "description": "주문·구매·좌석 선택 (예: 항공권·공연 티켓 구매, 단체 주문)",
        "ai_fit": ["assist", "unfit"],
        "themes": ["좌석·재고 동시 선점", "결제 실패·중복 결제", "취소·환불 수수료 규칙", "가격 반올림·할인 중복 적용", "구매 수량 제한", "개인정보 보관"],
    },
    "approval_workflow": {
        "description": "신청·승인·결재 흐름 (예: 휴가·비품·출장 신청, 시설 사용 허가)",
        "ai_fit": ["assist", "unfit"],
        "themes": ["결재선 예외(부재·대결)", "금액·기간별 승인 단계", "반려 후 재신청", "신청 마감 규칙", "감사용 이력 보관", "열람 권한"],
    },
    "settlement_billing": {
        "description": "정산·요금 계산 (예: 수수료 정산, 이용 요금 청구, 강사료 계산)",
        "ai_fit": ["unfit"],
        "themes": ["요금표 개정 시점(옛 요율 vs 새 요율)", "반올림·절사 규칙", "부분 환불·일할 계산", "세금·수수료 포함 여부", "정산 마감일", "검산 가능한 내역서"],
    },
    "shift_assignment": {
        "description": "인력 배치·근무표 편성 (예: 교대 근무표, 봉사자 배정, 강의실 배정)",
        "ai_fit": ["assist", "unfit"],
        "themes": ["연속 근무 제한", "선호·불가 일정", "자격·숙련도 조건", "공평성 기준", "급한 결원 대체", "확정 후 변경 규칙"],
    },
    "status_tracking": {
        "description": "접수·처리 현황 추적 (예: AS 접수, 민원 처리 현황, 분실물 관리)",
        "ai_fit": ["assist", "unfit"],
        "themes": ["상태 단계 정의", "처리 기한(SLA)", "고객에게 보이는 정보 범위", "담당자 이관", "중복 접수 병합", "알림 시점"],
    },
}

# 기술 조건: README '## 과제'에 명시하는 사용 도구·형태 조건. None이면 자유.
TECH_REQUIREMENTS = [
    None, None,
    "데이터는 서버를 재시작해도 남아야 한다(파일 기반 DB 등).",
    "제공된 데이터 파일을 원천 데이터로 그대로 읽어 써야 한다(형식 변환해 다시 저장하는 것은 응시자 판단).",
    "기존 사내 시스템에 다시 올릴 수 있도록 결과를 CSV로 내려받을 수 있어야 한다.",
    "현장 직원이 휴대폰 브라우저로 주로 사용한다.",
    "여러 사람이 동시에 사용하는 상황을 가정한다.",
    "관리자와 일반 사용자의 화면 또는 권한이 구분되어야 한다.",
]

# 각 값은 (최소, 최대). 실제 개수는 이 범위에서 무작위로 고른다.
DIFFICULTY = {
    "easy": {"facts": (6, 8), "conflict": (0, 1), "constraint": (1, 1), "data_files": (0, 1), "traps": (2, 2)},
    "standard": {"facts": (9, 12), "conflict": (1, 2), "constraint": (2, 2), "data_files": (1, 2), "traps": (3, 3)},
    "hard": {"facts": (13, 16), "conflict": (2, 3), "constraint": (3, 3), "data_files": (2, 4), "traps": (4, 4)},
}

README_SECTIONS = ["상황", "현업의 요청", "현업 파트너", "과제", "실행 환경"]
README_LEN = (400, 1300)
MAX_ATTEMPTS = 3  # 최초 1회 + 재생성 2회


class GenerationError(RuntimeError):
    pass


# ---------- 선택 ----------

def pick(domain: str | None, archetype: str | None, difficulty: Difficulty,
         seed: int | None, used_domains: set[str], recent_archetypes: list[str] = ()) -> dict:
    """recent_archetypes: 최근 세션의 과제 유형(최신순). 랜덤 선택 시 최근 3개는 피한다."""
    rng = random.Random(seed)
    if not domain:
        fresh = [d for d in DOMAINS if d not in used_domains] or DOMAINS
        domain = rng.choice(fresh)
    if not archetype:
        recent = set(recent_archetypes[:3])
        archetype = rng.choice([a for a in ARCHETYPES if a not in recent] or list(ARCHETYPES))
    if archetype not in ARCHETYPES:
        raise ValueError(f"알 수 없는 과제 유형: {archetype}")
    a = ARCHETYPES[archetype]
    ai_fit = rng.choice(a["ai_fit"])
    p = DIFFICULTY[difficulty]
    counts = {k: rng.randint(*v) for k, v in p.items()}
    themes = rng.sample(a["themes"], min(len(a["themes"]), 3 if difficulty == "easy" else 4))
    # 데이터 파일이 없는 출제에는 "제공된 데이터 파일" 조건을 걸지 않는다.
    techs = [t for t in TECH_REQUIREMENTS if counts["data_files"] or not (t or "").startswith("제공된 데이터")]
    return {"domain": domain, "archetype": archetype, "difficulty": difficulty, "ai_fit": ai_fit,
            "tech_requirement": rng.choice(techs), "themes": themes, **counts}


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

def _example_readme(ai_fit: str) -> str:
    """LLM 중심 과제는 기존 예시, 업무 시스템 과제는 업무 시스템 예시로 쏠림을 줄인다."""
    name = "example_readme.md" if ai_fit == "core" else "example_readme_system.md"
    return (prompts.PROMPT_DIR / name).read_text(encoding="utf-8")


def generate(choice: dict) -> GeneratedProblem:
    feedback = ""
    errors: list[str] = []
    for _ in range(MAX_ATTEMPTS):
        system = prompts.load(
            "generate_problem",
            domain=choice["domain"],
            archetype=choice["archetype"],
            archetype_description=ARCHETYPES[choice["archetype"]]["description"],
            ai_fit=AI_FIT[choice["ai_fit"]],
            tech_requirement=choice["tech_requirement"] or "없음 (구현 방식은 자유)",
            themes=", ".join(choice["themes"]),
            example_readme=_example_readme(choice["ai_fit"]),
            difficulty=choice["difficulty"],
            fact_count=choice["facts"],
            conflict_count=choice["conflict"],
            constraint_count=choice["constraint"],
            data_file_count=choice["data_files"],
            trap_count=choice["traps"],
            duration_text=prompts.duration_text(settings.duration_min),
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
