"""현업 담당자 채팅 (5.3). 시스템 프롬프트는 봉인 사실에서 코드로 결정적으로 렌더링한다."""
import json

from . import llm, prompts, sessions
from .config import settings
from .schemas import DisclosureJudgment, Fact, ProblemPublic, Sealed

CHAT = "chat.jsonl"


def _lines(facts: list[Fact], with_condition: str | None) -> str:
    out = []
    for f in facts:
        if with_condition == "reveal_when":
            out.append(f"- [{f.id}] {f.statement} (답하는 조건: {f.reveal_when})")
        elif with_condition == "detail":
            out.append(f"- [{f.id}] {f.statement} (최근 사례나 구체적인 장면을 물을 때 자연스럽게 언급)")
        else:
            out.append(f"- [{f.id}] {f.statement}")
    return "\n".join(out) or "(없음)"


def render_system(problem: ProblemPublic, sealed: Sealed) -> str:
    by = lambda t: [f for f in sealed.facts if f.type == t]  # noqa: E731
    p = sealed.persona_private
    return prompts.load(
        "persona_system",
        persona_name=problem.persona_public.name,
        persona_role=problem.persona_public.role,
        background=p.background,
        speaking_style=p.speaking_style,
        answerable_topics=", ".join(p.answerable_topics),
        ask_to_reveal=_lines(by("ask_to_reveal"), "reveal_when"),
        detail=_lines(by("detail"), "detail") if by("detail") else "",
        conflict=_lines(by("conflict"), None),
        confirm_only=_lines(by("confirm_only"), None),
        buried=_lines(by("buried"), None),
        readme=problem.readme_markdown,
        constraint=_lines(by("constraint"), "reveal_when"),
        priority_confusion="\n".join(f"- {c}" for c in sealed.priority_confusion) or "(없음)",
    )


def opening(session_id: str) -> None:
    """응시 시작 시 담당자 첫 인사. 공개 정보(persona_public.intro)를 그대로 써서 LLM 호출을 아낀다."""
    if sessions.read_jsonl(session_id, CHAT):
        return
    s = sessions.load_session(session_id)
    intro = sessions.load_problem(session_id).persona_public.intro
    sessions.append_jsonl(session_id, CHAT, {
        "ts": sessions.now_iso(), "elapsed_sec": sessions.elapsed_sec(s), "role": "persona",
        "text": intro, "disclosed_fact_ids": [], "question_quality": None,
    })


def _history_messages(log: list[dict]) -> list[dict]:
    return [{"role": "user" if r["role"] == "user" else "assistant", "content": r["text"]} for r in log]


def judge(sealed: Sealed, log_before: list[dict], user_text: str, reply: str) -> DisclosureJudgment:
    facts_json = json.dumps([{"id": f.id, "statement": f.statement} for f in sealed.facts], ensure_ascii=False)
    recent = "\n".join(f"{r['role']}: {r['text']}" for r in log_before[-8:]) or "(없음)"
    system = prompts.load("judge_disclosure", facts_json=facts_json, recent_context=recent,
                          user_message=user_text, persona_reply=reply)
    j = llm.parse(model=settings.model_fast, system=system, user="판정하라.",
                  schema=DisclosureJudgment, purpose="judge_disclosure")
    known = {f.id for f in sealed.facts}
    j.disclosed_fact_ids = [i for i in j.disclosed_fact_ids if i in known]
    j.partial_fact_ids = [i for i in j.partial_fact_ids if i in known and i not in j.disclosed_fact_ids]
    j.question_specificity = min(5, max(1, j.question_specificity))
    return j


def send(session_id: str, text: str) -> dict:
    """한 턴 처리: 사용자 메시지 저장 → 페르소나 응답 → 공개 판정 → 기록. 판정은 반환하지 않는다.

    직전 기록이 같은 내용의 미응답 user 메시지면(이전 시도 실패 후 재전송) 다시 저장하지 않는다.
    """
    s = sessions.load_session(session_id)
    log = sessions.read_jsonl(session_id, CHAT)
    if not (log and log[-1]["role"] == "user" and log[-1]["text"] == text):
        rec = {"ts": sessions.now_iso(), "elapsed_sec": sessions.elapsed_sec(s), "role": "user", "text": text}
        sessions.append_jsonl(session_id, CHAT, rec)
        log.append(rec)

    problem, sealed = sessions.load_problem(session_id), sessions.load_sealed(session_id)
    messages = [{"role": "system", "content": render_system(problem, sealed)}] + _history_messages(log)
    reply = llm.chat(model=settings.model_fast, messages=messages, purpose="persona")

    try:
        j = judge(sealed, log[:-1], text, reply)
        disclosure = {
            "disclosed_fact_ids": j.disclosed_fact_ids,
            "partial_fact_ids": j.partial_fact_ids,
            "question_quality": {
                "specificity": j.question_specificity, "is_dump_request": j.is_dump_request,
                "is_confirmation": j.is_confirmation, "is_scope_proposal": j.is_scope_proposal,
                "scope_agreed": j.scope_agreed,
            },
        }
    except llm.LLMError as e:  # 판정 실패가 대화를 막지 않는다
        disclosure = {"disclosed_fact_ids": [], "partial_fact_ids": [], "question_quality": None,
                      "judge_error": str(e)}

    out = {"ts": sessions.now_iso(), "elapsed_sec": sessions.elapsed_sec(s), "role": "persona", "text": reply}
    sessions.append_jsonl(session_id, CHAT, {**out, **disclosure})
    return out


def public_log(session_id: str) -> list[dict]:
    """화면용: 판정 필드를 제거한 대화."""
    return [{k: r[k] for k in ("ts", "elapsed_sec", "role", "text")} for r in sessions.read_jsonl(session_id, CHAT)]


def question_count(session_id: str) -> int:
    return sum(r["role"] == "user" for r in sessions.read_jsonl(session_id, CHAT))
