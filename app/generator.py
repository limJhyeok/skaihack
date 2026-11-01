"""출제 엔진 (5.1): 도메인·유형·난이도·AI 적합도·기술 조건·숨은 사실 주제·설계 개념 선택 → LLM 출제 → 코드로 출력 검증 → 실패 시 최대 2회 재생성."""
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
# facts: 숨은 사실(대화로만 드러남) 수. buried: README 안에 묻힌 사실 수(별도).
# counterintuitive: 직관에 어긋나는 업무 규칙 수. design_constraint: 설계 방향을 바꾸는 제약 수.
DIFFICULTY = {
    "easy": {"facts": (6, 8), "conflict": (0, 1), "constraint": (1, 1), "data_files": (0, 1), "traps": (2, 2),
             "buried": (2, 3), "counterintuitive": (1, 1), "design_constraint": (1, 1)},
    "standard": {"facts": (9, 12), "conflict": (1, 2), "constraint": (2, 2), "data_files": (1, 2), "traps": (3, 3),
                 "buried": (3, 4), "counterintuitive": (2, 2), "design_constraint": (1, 1)},
    "hard": {"facts": (13, 16), "conflict": (2, 3), "constraint": (3, 3), "data_files": (2, 4), "traps": (4, 4),
             "buried": (4, 6), "counterintuitive": (3, 3), "design_constraint": (1, 2)},
}

# README 길이 (A4 3~6장). target은 프롬프트 요청, accept는 검증 허용 범위(LLM 길이 오차 여유).
README_LEN = {
    "easy": {"target": (6000, 8000), "accept": (5000, 16000)},
    "standard": {"target": (8000, 11000), "accept": (6500, 16000)},
    "hard": {"target": (10000, 14000), "accept": (8000, 17000)},
}

# 설계 개념 축 개수 (docs/flow_drills_design.md 5.2). easy는 보조 1개만, hard는 핵심 1 + 보조 2.
CONCEPT_MIX = {
    "easy": {"core": 0, "light": 1, "qa_only": 0},
    "standard": {"core": 1, "light": 1, "qa_only": 1},
    "hard": {"core": 1, "light": 2, "qa_only": 2},
}

_BOOKING = ["reservation_booking", "route_scheduling", "shift_assignment"]

# 설계 개념 카드. 과제 유형과 별개 축. tier: core / light / qa_only / trap_only.
# family가 같은 개념은 한 문항에 하나만, heavy 개념은 한 문항에 하나만 (2시간 안에 이해하며 구현 가능한 양).
CONCEPTS = {
    "state_transition": {
        "name": "상태 전이와 부수효과", "tier": "core", "family": None, "heavy": False,
        "fit_archetypes": ["classification_triage", "content_generator_with_rules", "workflow_automation",
                           "reservation_booking", "inventory_ordering", "route_scheduling", "order_payment",
                           "approval_workflow", "shift_assignment", "status_tracking"],
        "policy_questions": ["중지·취소된 대상에 이미 걸린 신청·배정은 어떻게 되나", "한번 넘어간 단계를 되돌릴 수 있나, 누가 할 수 있나",
                             "단계가 바뀔 때 함께 일어나야 하는 일(알림, 대기자 처리, 재고 복원 등)은 무엇인가"],
        "naive_failure": "상태를 자유 문자열로 덮어써서 금지된 전이(완료→접수 등)가 열리고, 전이에 딸린 후속 처리(기존 신청 정리 등)가 빠진다.",
        "simplification": "허용 전이 표 하나와 전이 전 검사 함수 하나, 전이마다 후속 처리 한두 개. 범용 워크플로 엔진은 필요 없음.",
        "plan": "허용 전이 표를 한곳에 두고 모든 변경이 그 검사를 거치게 하며, 금지 전이·후속 처리 테스트를 추가한다.",
        "qa_seeds": ["금지된 단계 이동을 어디서 막는가", "단계가 바뀔 때 이미 걸려 있던 신청·배정은 어떻게 처리되는가", "되돌리기를 허용(또는 금지)한 근거"],
    },
    "template_instance": {
        "name": "반복 규칙과 날짜별 예외", "tier": "core", "family": None, "heavy": True,
        "fit_archetypes": _BOOKING + ["workflow_automation"],
        "policy_questions": ["기본 반복 규칙(요일·시간)과 공휴일·임시 일정이 겹치면 무엇이 우선인가",
                             "자정을 넘기는 회차는 어느 날짜에 속하나", "이미 확정된 날짜에 규칙이 바뀌면 소급하나"],
        "naive_failure": "반복 규칙만 보고 날짜별 예외(공휴일 운휴, 임시 증편)를 반영하지 않거나, 예외 적용 순서가 뒤바뀌어 운휴일에 일정이 생긴다.",
        "simplification": "요일 규칙 + 공휴일 목록 + 날짜별 예외 표. 예외 우선순위를 한 함수에서 정한다. 범용 반복 규칙(RRULE) 전체 구현은 과함.",
        "plan": "조회 시 계산(규칙→공휴일→날짜별 예외 순)으로 통일하고 예외가 겹치는 날짜를 테스트로 고정한다. 미리 펼쳐 저장하는 방식과의 트레이드오프를 문서화한다.",
        "qa_seeds": ["반복 일정을 미리 펼쳐 저장하지 않고(또는 저장하고) 조회 때 계산한 이유", "공휴일과 임시 일정이 겹칠 때 적용 순서", "규칙이 바뀌면 이미 만들어진 일정은 어떻게 되는가"],
    },
    "effective_period": {
        "name": "기준 개정과 적용 시점", "tier": "core", "family": None, "heavy": True,
        "fit_archetypes": ["settlement_billing", "route_scheduling", "inventory_ordering", "order_payment",
                           "document_qa_assistant", "classification_triage", "content_generator_with_rules",
                           "text_feedback_tool", "voice_practice_service"],
        "policy_questions": ["기준(요율·규정·분류·노선)이 바뀐 날 이전 건은 옛 기준으로 봐야 하나",
                             "지난 기록을 다시 볼 때 당시 기준이 보여야 하나 지금 기준이 보여야 하나", "개정이 미리 등록되고 나중에 시행되나"],
        "naive_failure": "기준을 덮어써서 지난 건을 다시 계산·조회하면 새 기준으로 바뀌어 보이고, 민원·정산 검증 때 당시 근거를 댈 수 없다.",
        "simplification": "개정이 잦은 엔티티 하나에만 시행일(valid_from) 열을 두고 '그 날짜에 유효한 기준'을 고르는 함수 하나. 모든 테이블 이력화는 과함.",
        "plan": "시행일 기준 조회 함수로 통일하고 개정일 경계(전날·당일) 테스트를 둔다. 대안(덮어쓰기+감사 로그, 계산 결과 스냅샷)과 비교해 근거를 남긴다.",
        "qa_seeds": ["지난달 건을 다시 조회하면 어떤 기준이 적용되는가", "기준 이력을 두는 대신 결과를 저장(스냅샷)하는 방식과 비교", "개정일 당일 건은 어느 기준인가"],
    },
    "csv_import": {
        "name": "엑셀(CSV) 일괄 등록", "tier": "core", "family": None, "heavy": True,
        "fit_archetypes": ["data_insight_dashboard", "inventory_ordering", "settlement_billing", "shift_assignment",
                           "route_scheduling", "classification_triage", "approval_workflow", "reservation_booking"],
        "policy_questions": ["같은 파일을 두 번 올리면 어떻게 돼야 하나", "몇 줄이 잘못됐을 때 나머지는 반영하나, 전부 거절하나",
                             "옛 양식·옛 코드가 섞인 행은 어떻게 하나"],
        "naive_failure": "재업로드 때 같은 행이 중복 등록되고, 중간 행 오류에서 앞부분만 반영된 채 멈춰 데이터가 반쯤 바뀐다.",
        "simplification": "담당자가 정한 방식 하나(전부 거절 / 오류 행만 제외 / 덮어쓰기)를 자연키 기준으로 구현하고 오류 행 목록을 보여 준다. 세 방식을 모두 만들어 고르게 하는 것은 과함.",
        "plan": "자연키로 멱등 업서트, 파일 단위 트랜잭션 또는 오류 행 보고를 두고 같은 파일 두 번 올리기·오류 행 섞기 테스트를 추가한다.",
        "qa_seeds": ["같은 파일을 두 번 올리면 무슨 일이 생기는가", "중간 행이 잘못됐을 때 어디까지 반영되는가, 그렇게 정한 근거", "행을 무엇으로 같은 건이라고 판단하는가"],
    },
    "business_date": {
        "name": "업무 기준일과 달력 날짜", "tier": "core", "family": None, "heavy": False,
        "fit_archetypes": ["route_scheduling", "shift_assignment", "settlement_billing", "data_insight_dashboard",
                           "reservation_booking", "status_tracking", "order_payment", "inventory_ordering"],
        "policy_questions": ["자정을 넘기는 근무·운행·주문은 어느 날짜로 기록·집계하나", "하루의 시작(마감) 시각은 몇 시인가",
                             "영업일·휴일은 기한 계산에 어떻게 들어가나"],
        "naive_failure": "실제 발생 시각의 달력 날짜로 기록·집계해서 자정을 넘긴 건이 다음 날로 넘어가고, 일별 합계·기한이 담당자 기준과 어긋난다.",
        "simplification": "발생 시각 → 업무 기준일 변환 함수 하나와 경계 시각 상수 하나. 기록에 업무 기준일을 함께 저장하거나 조회 때 계산.",
        "plan": "업무 기준일 변환을 한 함수로 모으고 경계 시각 전후(23:59, 00:00, 기준 시각) 테스트를 둔다.",
        "qa_seeds": ["자정을 넘긴 건이 어느 날짜로 잡히는가, 그 근거", "기준 시각이 바뀌면 고칠 곳", "업무 기준일을 저장하는 것과 매번 계산하는 것의 차이"],
    },
    "capacity_waitlist": {
        "name": "정원과 대기 순번 승격", "tier": "core", "family": None, "heavy": False,
        "fit_archetypes": ["reservation_booking", "route_scheduling", "order_payment", "shift_assignment", "approval_workflow"],
        "policy_questions": ["정원이 차면 거절하나 대기로 받나", "취소가 나면 대기자는 자동으로 올라가나, 누구부터인가",
                             "같은 시각 신청·우선권(직급·부서·장애 등)이 겹치면 순서는"],
        "naive_failure": "정원 확인과 등록이 떨어져 있어 정원을 넘기고, 취소 때 대기자 승격이 빠지거나 순서가 매번 달라진다.",
        "simplification": "정원 검사와 등록을 한 처리로 묶고, 취소 시 대기 1순위를 올리는 함수 하나. 순서는 신청 시각 + 결정적 동점 기준.",
        "plan": "정원 검사·등록을 한 트랜잭션(조건부 갱신)으로 묶고 취소→승격, 동점 순서 테스트를 추가한다.",
        "qa_seeds": ["정원이 찬 순간 두 사람이 동시에 신청하면", "취소 후 대기자가 올라가는 순서와 그 근거", "승격된 사람이 원치 않으면 어떻게 되는가"],
    },
    "interval_overlap": {
        "name": "시간 구간 겹침", "tier": "light", "family": None, "heavy": False,
        "fit_archetypes": _BOOKING + ["workflow_automation", "order_payment"],
        "policy_questions": ["앞 일정이 끝나는 시각에 다음 일정을 바로 시작해도 되나", "일정 사이에 비워 둬야 하는 시간이 있나"],
        "naive_failure": "겹침 판정에서 경계(끝=시작)를 잘못 다루거나, 자정을 넘는 구간·준비 시간을 빼먹어 중복 배정이 생긴다.",
        "simplification": "시작 포함·끝 제외 구간 하나의 비교식과 준비 시간 상수.",
        "plan": "겹침 판정을 한 함수로 모으고 끝=시작, 자정 넘김, 준비 시간 경계 테스트를 추가한다.",
        "qa_seeds": ["끝나는 시각과 시작 시각이 같으면 겹치는가, 그 근거", "자정을 넘는 일정의 겹침은 어떻게 판정하는가"],
    },
    "permissions": {
        "name": "역할별 허용 동작", "tier": "light", "family": None, "heavy": False, "fit_archetypes": None,
        "policy_questions": ["누가 무엇을 볼 수 있고 무엇을 바꿀 수 있나", "본인 것이 아닌 건을 대신 처리할 수 있는 사람은"],
        "naive_failure": "화면에서 버튼만 숨기고 서버는 검사하지 않아, 요청을 직접 보내면 권한 없는 사용자가 바꿀 수 있다.",
        "simplification": "역할 2~3개와 동작별 허용 표, 서버에서 한 번 검사. 정교한 로그인 체계는 필요 없음(역할 선택·간단 토큰도 근거와 함께 허용).",
        "plan": "서버 쪽에 역할 검사를 한곳에 두고, 권한 없는 역할로 직접 요청하는 테스트를 추가한다.",
        "qa_seeds": ["권한 검사를 어디서 하는가, 화면만 막으면 무엇이 문제인가", "로그인을 간소화한 근거와 그 한계"],
    },
    "idempotency": {
        "name": "같은 요청의 반복", "tier": "light", "family": None, "heavy": False, "fit_archetypes": None,
        "policy_questions": ["같은 신청·등록을 두 번 누르면 어떻게 돼야 하나", "한 사람이 같은 건을 중복으로 가질 수 있나"],
        "naive_failure": "새로고침·두 번 클릭으로 같은 신청이 두 건 생기고, 집계·정원이 틀어진다.",
        "simplification": "업무상 같은 건을 가르는 키 하나로 중복 등록을 막는다(DB 유일 조건 또는 등록 전 조회).",
        "plan": "자연키 유일 조건을 두고 같은 요청 두 번 보내기 테스트를 추가한다.",
        "qa_seeds": ["같은 요청이 두 번 오면 무슨 일이 생기는가", "중복을 무엇으로 판단하는가"],
    },
    "deterministic_tiebreak": {
        "name": "동점·같은 조건일 때의 순서", "tier": "light", "family": None, "heavy": False,
        "fit_archetypes": ["classification_triage", "reservation_booking", "route_scheduling", "shift_assignment",
                           "order_payment", "data_insight_dashboard", "approval_workflow", "inventory_ordering",
                           "settlement_billing", "status_tracking"],
        "policy_questions": ["조건이 똑같은 두 건이면 누구를 먼저 처리하나", "순위·배정 결과가 매번 같아야 하나"],
        "naive_failure": "정렬·배정 기준이 동점일 때 저장 순서·해시 순서에 맡겨져 실행할 때마다 결과가 달라지고 설명할 수 없다.",
        "simplification": "업무 기준 뒤에 고정 보조 키(신청 시각, ID)를 붙인 정렬 한 줄.",
        "plan": "동점 데이터를 넣고 두 번 실행해 결과가 같은지 확인하는 테스트를 추가한다.",
        "qa_seeds": ["조건이 같은 두 건의 순서는 무엇으로 정해지는가", "결과가 실행마다 같다는 것을 어떻게 확인했는가"],
    },
    "soft_delete": {
        "name": "삭제와 기존 기록의 연결", "tier": "light", "family": None, "heavy": False, "fit_archetypes": None,
        "policy_questions": ["더 이상 안 쓰는 항목(정류장·장비·담당자·상품)을 지우면 지난 기록은 어떻게 보여야 하나", "지운 항목을 다시 살릴 수 있나"],
        "naive_failure": "참조되는 항목을 실제로 지워 지난 기록이 깨지거나(이름이 사라짐), 지운 항목이 새 신청 화면에 계속 나온다.",
        "simplification": "사용 중지 표시 하나로 새 선택에서만 빼고 지난 기록은 그대로 보이게 한다.",
        "plan": "사용 중지 항목이 새 선택에선 빠지고 지난 기록에는 보이는지 테스트한다.",
        "qa_seeds": ["항목을 지우면 지난 기록은 어떻게 보이는가", "실제 삭제 대신(또는 대로) 처리한 근거"],
    },
    "uniqueness_capacity": {
        "name": "동시 신청과 중복·정원 보장", "tier": "qa_only", "family": "concurrency", "heavy": False,
        "fit_archetypes": ["reservation_booking", "route_scheduling", "order_payment", "shift_assignment", "inventory_ordering"],
        "policy_questions": [],
        "naive_failure": "앱 코드에서 '남은 자리 확인 → 저장'을 따로 해서 동시 요청 때 정원을 넘거나 같은 자리가 두 번 팔린다. 유일 조건은 '정원 N명'을 막지 못한다.",
        "simplification": "구현 요구 아님. 한 프로세스·파일 DB면 위험이 작다는 근거를 댈 수 있으면 충분.",
        "plan": "조건부 갱신(남은 수 > 0일 때만 감소)이나 트랜잭션, 취소 후 재신청을 위한 부분 유일 조건. 동시 요청 테스트로 확인.",
        "qa_seeds": ["두 사람이 마지막 한 자리를 동시에 신청하면", "DB 유일 조건과 앱의 사전 확인 중 무엇에 의존하는가, 그 한계"],
    },
    "optimistic_lock": {
        "name": "동시 수정 충돌", "tier": "qa_only", "family": "concurrency", "heavy": False, "fit_archetypes": None,
        "policy_questions": [],
        "naive_failure": "두 관리자가 같은 건을 열어 고치면 나중에 저장한 쪽이 앞사람의 변경을 말없이 덮어쓴다.",
        "simplification": "구현 요구 아님. 마지막 저장 우선을 허용한 근거와 한계를 말할 수 있으면 충분.",
        "plan": "버전 열을 두고 저장 시 버전이 다르면 거절하고 다시 불러오게 한다(ETag 등).",
        "qa_seeds": ["두 관리자가 같은 건을 동시에 고치면 어떻게 되는가", "마지막 저장 우선을 허용해도 되는 근거와 위험"],
    },
    "audit_log": {
        "name": "변경 이력", "tier": "qa_only", "family": None, "heavy": False, "fit_archetypes": None,
        "policy_questions": [],
        "naive_failure": "값을 덮어써서 누가 언제 무엇을 바꿨는지 남지 않아, 민원·정산 이의 때 근거를 댈 수 없다.",
        "simplification": "구현 요구 아님. 필요해지는 상황과 최소 형태(누가·언제·무엇을·전/후)를 말할 수 있으면 충분.",
        "plan": "중요 변경에만 이력 행(누가·언제·전/후 값)을 남기고, 기준 이력(시행일)과 어느 쪽이 맞는지 비교한다.",
        "qa_seeds": ["누가 언제 바꿨는지 물으면 어떻게 답하는가", "이력을 남기지 않은 근거와 한계"],
    },
}

# 2시간 범위에서 만들면 감점되는 개념. 과설계 함정 후보로만 쓴다.
TRAP_CONCEPTS = ["조회 결과 캐싱 계층", "메시지 큐·비동기 작업자", "서비스를 여러 개로 쪼갠 구조(마이크로서비스)",
                 "대량 데이터용 페이지네이션·인덱스 최적화"]

# README에 쓰면 안 되는 개념 이름·실습 지시 문구 (개념은 업무 말로만 드러나야 한다).
BANNED_README_TERMS = ["상태 머신", "상태머신", "상태 전이", "낙관적 락", "비관적 락", "경쟁 상태", "레이스 컨디션",
                       "race condition", "멱등", "idempotent", "재현하라", "재현해 보", "재현하세요", "템플릿-인스턴스",
                       "valid_from", "valid_to", "반열린 구간", "소프트 삭제", "soft delete", "동시성 제어", "동시성 처리",
                       "optimistic lock", "state machine"]

README_SECTIONS = ["상황", "현업의 요청", "현업 파트너", "과제", "업무 정책", "데이터 명세", "예외 처리", "제약 사항", "실행 환경"]
MAX_ATTEMPTS = 3  # 최초 1회 + 재생성 2회


class GenerationError(RuntimeError):
    pass


# ---------- 선택 ----------

def _fits(concept_id: str, archetype: str) -> bool:
    fit = CONCEPTS[concept_id]["fit_archetypes"]
    return fit is None or archetype in fit


def pick_concepts(archetype: str, difficulty: Difficulty, rng: random.Random,
                  recent_core: list[str] = ()) -> dict[str, list[str]]:
    """난이도별 개수(CONCEPT_MIX)대로 core → light → qa_only 순으로 뽑는다.

    규칙: 같은 family(동시성)는 하나만, heavy 개념은 하나만. 최근 2세션의 핵심 개념은 피한다.
    """
    mix = CONCEPT_MIX[difficulty]
    chosen: dict[str, list[str]] = {"core": [], "light": [], "qa_only": []}
    taken: list[str] = []

    def allowed(k: str) -> bool:
        c = CONCEPTS[k]
        if k in taken:
            return False
        if c["heavy"] and any(CONCEPTS[t]["heavy"] for t in taken):
            return False
        return not (c["family"] and any(CONCEPTS[t]["family"] == c["family"] for t in taken))

    for tier in ("core", "light", "qa_only"):
        pool = [k for k, c in CONCEPTS.items() if c["tier"] == tier and _fits(k, archetype)]
        if tier == "core":
            recent = set(recent_core[:2])
            pool = [k for k in pool if k not in recent] or pool
        for _ in range(mix[tier]):
            opts = [k for k in pool if allowed(k)]
            if not opts:
                break
            k = rng.choice(opts)
            chosen[tier].append(k)
            taken.append(k)
    return chosen


def pick(domain: str | None, archetype: str | None, difficulty: Difficulty,
         seed: int | None, used_domains: set[str], recent_archetypes: list[str] = (),
         recent_core_concepts: list[str] = ()) -> dict:
    """recent_archetypes: 최근 세션의 과제 유형(최신순). 랜덤 선택 시 최근 3개는 피한다.
    recent_core_concepts: 최근 세션의 핵심 개념(최신순). 최근 2개는 피한다."""
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
    tech = rng.choice(techs)
    concepts = pick_concepts(archetype, difficulty, rng, list(recent_core_concepts))
    return {"domain": domain, "archetype": archetype, "difficulty": difficulty, "ai_fit": ai_fit,
            "tech_requirement": tech, "themes": themes, "concepts": concepts, **counts}


def concepts_block(concepts: dict[str, list[str]]) -> str:
    """출제 프롬프트의 {{concepts}}. 개념 이름은 출제자용이며 README에는 쓰지 않는다."""
    role_text = {
        "core": "핵심 — README 정책 + 숨은 사실(critical) + README에 묻힌 사실에 나눠 심는다",
        "light": "보조 — README 정책 또는 숨은 사실 중 한 곳 이상에 업무 말로 심는다",
        "qa_only": "Q&A 전용 — README·숨은 사실에 요구로 넣지 않는다. good_solution_criteria에 판단 근거 한 줄, concepts에 qa_seeds",
    }
    out = []
    for role in ("core", "light", "qa_only"):
        for k in concepts.get(role, []):
            c = CONCEPTS[k]
            lines = [f"### [{role}] id={k} · {c['name']}", f"- 역할: {role_text[role]}"]
            if c["policy_questions"]:
                lines.append("- 담당자가 업무 판단으로 알고 있을 것: " + " / ".join(c["policy_questions"]))
            lines += [f"- 순진한 구현의 결함: {c['naive_failure']}",
                      f"- 2시간 간소화 허용선: {c['simplification']}",
                      f"- 개선안: {c['plan']}",
                      f"- 서술형 질문 씨앗: " + " / ".join(c["qa_seeds"])]
            out.append("\n".join(lines))
    if not out:
        return "(이번 출제에는 설계 개념 축이 없다. concepts는 빈 배열)"
    return "\n\n".join(out)


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


def banned_terms(readme: str) -> list[str]:
    low = readme.lower()
    return [t for t in BANNED_README_TERMS if t.lower() in low]


def _validate_concepts(p: GeneratedProblem, chosen: dict[str, list[str]]) -> list[str]:
    errors: list[str] = []
    facts = {f.id: f for f in p.facts}
    by_id = {c.id: c for c in p.concepts}
    for c in p.concepts:
        if c.id not in CONCEPTS:
            errors.append(f"concepts: 알 수 없는 개념 id '{c.id}'")
        missing = [i for i in c.fact_ids if i not in facts]
        if missing:
            errors.append(f"concepts[{c.id}]: 없는 사실 id {', '.join(missing)}")
    for role, ids in chosen.items():
        for k in ids:
            c = by_id.get(k)
            if c is None:
                errors.append(f"concepts에 '{k}'({role})가 없음")
                continue
            if c.role != role:
                errors.append(f"concepts[{k}]: role은 {role}이어야 함 (지금 {c.role})")
            linked = [facts[i] for i in c.fact_ids if i in facts]
            if role == "core":
                if len(set(c.placements)) < 2 or not {"readme", "fact"} <= set(c.placements):
                    errors.append(f"concepts[{k}]: 핵심 개념은 placements에 readme와 fact를 모두 포함해야 함")
                if not any(f.type != "buried" and f.importance == "critical" for f in linked):
                    errors.append(f"concepts[{k}]: 연결된 critical 숨은 사실이 없음")
                if not any(f.type == "buried" for f in linked):
                    errors.append(f"concepts[{k}]: 연결된 묻힌 사실(buried)이 없음")
            elif role == "light":
                if not c.placements:
                    errors.append(f"concepts[{k}]: placements가 비어 있음")
            if not c.qa_seeds:
                errors.append(f"concepts[{k}]: qa_seeds가 비어 있음")
    return errors


def validate(p: GeneratedProblem, choice: dict | None = None) -> list[str]:
    """choice가 있으면 난이도별 길이·묻힌 사실 수·개념 배치까지 검사한다."""
    errors: list[str] = []
    difficulty = (choice or {}).get("difficulty", "standard")
    headings = [m.strip() for m in re.findall(r"^##\s+(.+)$", p.readme_markdown, flags=re.M)]
    for sec in README_SECTIONS:
        if not any(h.startswith(sec) for h in headings):
            errors.append(f"README에 '## {sec}' 섹션이 없음")
    n = readme_length(p.readme_markdown)
    lo, hi = README_LEN[difficulty]["accept"]
    if not lo <= n <= hi:
        errors.append(f"README 길이 {n}자 (허용 {lo}~{hi}자)")
    bad = banned_terms(p.readme_markdown)
    if bad:
        errors.append("README에 개념 이름·실습 지시 문구가 있음 (업무 말로 바꿀 것): " + ", ".join(bad))
    hidden = [f for f in p.facts if f.type != "buried"]
    buried = [f for f in p.facts if f.type == "buried"]
    if sum(f.importance == "critical" for f in hidden) < 2:
        errors.append("critical 숨은 사실(buried 제외)이 2개 미만")
    if choice and len(buried) < max(1, choice.get("buried", 1) - 1):
        errors.append(f"묻힌 사실(buried)이 {len(buried)}개 (요구 {choice.get('buried')}개)")
    data_texts = [d.content for d in p.data_files]
    readme_norm = _norm(p.readme_markdown)
    for f in p.facts:
        if not f.reveal_when.strip() or not f.example_question.strip():
            errors.append(f"{f.id}: reveal_when 또는 example_question 누락")
        if f.type == "buried":
            q = _norm(f.readme_quote or "")
            if len(q) < 10 or q not in readme_norm:
                errors.append(f"{f.id}: 묻힌 사실의 readme_quote가 README 원문 그대로가 아니거나 너무 짧음")
        elif leaks(f.statement, p.readme_markdown, data_texts):
            errors.append(f"{f.id}: 사실 내용이 README/데이터에 노출됨")
    for d in p.data_files:
        if not d.path.startswith("data/") or ".." in d.path:
            errors.append(f"데이터 파일 경로가 data/ 아래가 아님: {d.path}")
    if choice and "concepts" in choice:
        errors += _validate_concepts(p, choice["concepts"])
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
            readme_min=README_LEN[choice["difficulty"]]["target"][0],
            readme_max=README_LEN[choice["difficulty"]]["target"][1],
            buried_count=choice.get("buried", 0),
            counterintuitive_count=choice.get("counterintuitive", 1),
            design_constraint_count=choice.get("design_constraint", 1),
            concepts=concepts_block(choice.get("concepts", {})),
            trap_concepts=", ".join(TRAP_CONCEPTS),
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
        errors = validate(problem, choice)
        if not errors:
            return problem
        # 실패 사유는 숨은 사실 내용 없이 항목명만 넘긴다.
        feedback = "\n[이전 출제가 검증에 실패한 이유 — 이번에는 반드시 고칠 것]\n" + "\n".join(f"- {e}" for e in errors) + "\n"
    raise GenerationError("출제 검증 실패 (재생성 2회 후): " + "; ".join(errors))
