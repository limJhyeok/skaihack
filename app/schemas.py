"""데이터 스키마. 같은 Pydantic 모델로 Structured Outputs의 JSON 스키마를 만든다."""
from typing import Literal

from pydantic import BaseModel

FactType = Literal["ask_to_reveal", "conflict", "confirm_only", "constraint", "detail"]
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


class Sealed(BaseModel):
    """sealed.bin 복호화 결과 (4.2)."""
    persona_private: PersonaPrivate
    facts: list[Fact]
    priority_confusion: list[str]
    good_solution_criteria: list[str]
    overengineering_traps: list[str]
    data_file_notes: str


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


# ---------- 평가 리포트 (5.9, 6.6, 6.7, 6.7-1, 6.8) ----------

Verdict = Literal["충족", "부분충족", "미충족"]


class EvalItem(BaseModel):
    key: str
    verdict: Verdict
    score: int
    rationale: str
    evidence: list[str]
    suggestion: str


class TechnicalEval(BaseModel):
    items: list[EvalItem]
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
