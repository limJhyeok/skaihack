"""데이터 스키마. 같은 Pydantic 모델로 Structured Outputs의 JSON 스키마를 만든다."""
from typing import Literal

from pydantic import BaseModel

FactType = Literal["ask_to_reveal", "conflict", "confirm_only", "constraint", "detail", "buried"]
ConceptRole = Literal["core", "light", "qa_only"]
ConceptPlacementKind = Literal["readme", "fact", "data"]
Importance = Literal["critical", "major", "minor"]
Difficulty = Literal["easy", "standard", "hard"]


class DataFile(BaseModel):
    path: str
    content: str


class PersonaPublic(BaseModel):
    name: str
    role: str
    intro: str


class PersonaPrivate(BaseModel):
    speaking_style: str
    answerable_topics: list[str]
    background: str


class Fact(BaseModel):
    id: str
    statement: str
    type: FactType
    importance: Importance
    reveal_when: str
    example_question: str
    # buried(README에 묻힌 사실)만: README 원문에서 그대로 옮긴 근거 문장. 그 외 유형은 null.
    readme_quote: str | None = None


class ConceptUse(BaseModel):
    """출제 LLM이 설계 개념을 실제로 어디에 심었는지 (docs/flow_drills_design.md 5.3)."""
    id: str
    role: ConceptRole
    placements: list[ConceptPlacementKind]
    fact_ids: list[str]
    naive_failure: str      # 이 문항에서 순진하게 구현하면 생기는 결함 (weak_point·impact 씨앗)
    plan: str               # 개선안 (plan 씨앗)
    qa_seeds: list[str]     # 서술형 질문 씨앗


class Sealed(BaseModel):
    """sealed.bin 복호화 결과 (4.2)."""
    persona_private: PersonaPrivate
    facts: list[Fact]
    priority_confusion: list[str]
    good_solution_criteria: list[str]
    overengineering_traps: list[str]
    data_file_notes: str
    # 출제 설정 (공개 전 숨김). 이전 세션에는 없어서 기본값을 둔다.
    ai_fit: str = ""
    tech_requirement: str | None = None
    themes: list[str] = []
    concepts: list[ConceptUse] = []


class GeneratedProblem(BaseModel):
    """출제 LLM 출력. 공개분과 봉인분으로 나눠 저장한다."""
    title: str
    readme_markdown: str
    data_files: list[DataFile]
    persona_public: PersonaPublic
    persona_private: PersonaPrivate
    facts: list[Fact]
    priority_confusion: list[str]
    good_solution_criteria: list[str]
    overengineering_traps: list[str]
    data_file_notes: str
    concepts: list[ConceptUse]


class ProblemPublic(BaseModel):
    """problem_public.json (4.1)."""
    problem_id: str
    title: str
    domain: str
    archetype: str
    difficulty: Difficulty
    readme_markdown: str
    data_files: list[DataFile]
    persona_public: PersonaPublic


class Session(BaseModel):
    """session.json (4.4)."""
    session_id: str
    problem_id: str
    workspace_path: str
    mode: Literal["full", "practice"] = "full"
    duration_min: int
    started_at: str | None = None
    submitted_at: str | None = None
    status: Literal["created", "running", "submitted", "rehearsed", "evaluated", "revealed"] = "created"
    agent_tool: Literal["codex", "claude_code", "other"] | None = None
    run_command: str | None = None
    app_port: int | None = None


class DisclosureJudgment(BaseModel):
    """공개 판정 출력 (6.3)."""
    disclosed_fact_ids: list[str]
    partial_fact_ids: list[str]
    question_specificity: int
    is_dump_request: bool
    is_confirmation: bool
    is_scope_proposal: bool
    scope_agreed: bool


# ---------- 설명 리허설 (5.8, 6.4, 6.5) ----------

RehearsalType = Literal["architecture", "design_reason", "failure_response", "risk_prediction", "requirement_mapping",
                        "data_flow", "ai_decision", "experiment_reasoning"]
CognitiveCategory = Literal["behavior_risk", "structure", "decision_rationale"]
StepVerdict = Literal["met", "partial", "missed"]


class AnswerStep(BaseModel):
    text: str
    evidence: list[str]  # "파일:줄" 근거 좌표


class ModelAnswer(BaseModel):
    weak_point: AnswerStep
    impact: AnswerStep
    plan: AnswerStep


class RehearsalQuestion(BaseModel):
    id: str
    type: RehearsalType
    category: CognitiveCategory
    question: str
    model_answer: ModelAnswer


class GeneratedQuestions(BaseModel):
    questions: list[RehearsalQuestion]


class GradeOutput(BaseModel):
    """채점 출력 (6.5). verdict는 코드가 steps로 계산한다."""
    weak_point: StepVerdict
    impact: StepVerdict
    plan: StepVerdict
    misconception: str | None
    revisit: list[str]
    comment: str


# ---------- 추적 카드 드릴 (docs/drill_design.md) ----------

class CardSlot(BaseModel):
    id: str                                  # "s1"…
    prompt: str
    kind: Literal["choice", "value"]
    options: list[str]                       # choice일 때 2~4개, value면 빈 배열
    claimed: str                             # 출제자가 예상한 정답 (실행 관찰값과 대조)


class GeneratedCard(BaseModel):
    id: str
    context: str                             # 기능 흐름 안내 (사용자 행동 → 결과, 용어 풀이). 정답을 정하는 처리는 쓰지 않음
    scenario: str
    slots: list[CardSlot]
    evidence: list[str]                      # "파일:줄"
    explanation: str
    defect_tags: list[str]
    probe: str


class GeneratedDeck(BaseModel):
    cards: list[GeneratedCard]


class RepoFile(BaseModel):
    path: str
    content: str


class PlantedDefect(BaseModel):
    tag: str                                 # 결함 카탈로그 ID
    location: str                            # "파일:줄" 또는 "파일:시작-끝"
    behavior: str                            # 그 결함 때문에 실제로 일어나는 일 (사용자 쪽 사건)


class FreshRepo(BaseModel):
    """새 문제 드릴용 저장소 (docs/drill_design.md §6). 결함 사양은 저장소 밖(덱 출제 입력)에만 둔다."""
    title: str
    files: list[RepoFile]
    planted: list[PlantedDefect]
    bait: PlantedDefect                      # 위험해 보이지만 실제로는 안전한 코드 (tag는 "BAIT")


class CardRepair(BaseModel):
    action: Literal["fix_probe", "accept_observed", "drop"]
    probe: str | None
    explanation: str | None
    reason: str


# ---------- 평가 리포트 (5.9, 6.6, 6.7, 6.7-1, 6.8) ----------

Verdict = Literal["충족", "부분충족", "미충족"]


class EvalItem(BaseModel):
    key: str
    verdict: Verdict
    score: int
    rationale: str
    evidence: list[str]
    suggestion: str


class PolicyCheck(BaseModel):
    """묻힌 사실·설계 개념이 결과물·문서·테스트에 반영됐는지 (공개 화면에서만 보여 줌)."""
    ref: str                # 사실 id(F..) 또는 개념 id
    verdict: Literal["반영", "부분 반영", "미반영"]
    evidence: list[str]
    note: str


class TechnicalEval(BaseModel):
    items: list[EvalItem]
    policy_checks: list[PolicyCheck]
    undiscovered_note: str
    strengths: list[str]
    improvements: list[str]


class CommunicationEval(BaseModel):
    verdict: Verdict
    score: int
    rationale: str
    evidence: list[str]


class IntentEval(BaseModel):
    items: list[EvalItem]
    cost_note: str
    best_moment: str
    worst_moment: str
    next_actions: list[str]
    communication: CommunicationEval


class ConsistencyClaim(BaseModel):
    doc: str
    claim: str
    verdict: Literal["consistent", "partially", "inconsistent", "not_verifiable"]
    evidence: list[str]
    note: str


class ConsistencyEval(BaseModel):
    claims: list[ConsistencyClaim]


class ReportSummary(BaseModel):
    one_liner: str
    top_strength: str
    top_weakness: str


class MissedFactAnalysis(BaseModel):
    fact_id: str
    closest_user_question: str | None
    why_missed: str
    better_question: str


class RevealMissed(BaseModel):
    items: list[MissedFactAnalysis]
