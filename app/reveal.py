"""정답 공개와 회고 (5.10, 6.8)."""
import json
from datetime import datetime

from . import evaluate, llm, metrics, prompts, sessions
from .config import settings
from .schemas import RevealMissed

TYPE_LABEL = {"ask_to_reveal": "물어야 나옴", "conflict": "자료와 충돌", "confirm_only": "확인만",
              "constraint": "제약·금지", "detail": "현장 디테일"}

AI_FIT_LABEL = {"core": "LLM이 핵심 기능", "assist": "LLM은 선택적 보조 기능",
                "unfit": "LLM이 맞지 않음 — 핵심 로직은 결정적으로 짜야 함"}


class RevealError(RuntimeError):
    pass


def reveal(session_id: str) -> dict:
    s = sessions.load_session(session_id)
    d = sessions.session_dir(session_id)
    if s.status not in ("evaluated", "revealed"):
        raise RevealError("평가 리포트를 먼저 생성하세요.")
    if (d / "reveal.json").exists():
        return load(session_id)
    sealed = sessions.load_sealed(session_id)
    chat = sessions.read_jsonl(session_id, "chat.jsonl")
    disc = metrics.discovery(sealed, chat)
    questions = [r["text"] for r in chat if r["role"] == "user"]
    missed = [f for f in sealed.facts if not disc[f.id]["discovered"]]

    analysis = {}
    if missed:
        out = llm.parse(
            model=settings.model_strong,
            system=prompts.load(
                "reveal_missed",
                missed_facts="\n".join(f"- {f.id}: {f.statement} ({f.type}; 드러나는 조건: {f.reveal_when})" for f in missed),
                user_questions="\n".join(f"{i}: {q}" for i, q in enumerate(questions, 1)) or "(질문 없음)"),
            user="분석하라.", schema=RevealMissed, purpose="reveal_missed")
        analysis = {a.fact_id: a.model_dump() for a in out.items}

    rows = []
    for f in sealed.facts:
        dd = disc[f.id]
        a = analysis.get(f.id, {})
        rows.append({
            "id": f.id, "statement": f.statement, "type": f.type, "type_label": TYPE_LABEL.get(f.type, f.type),
            "importance": f.importance, "discovered": dd["discovered"], "partial": dd["partial"],
            "question_no": dd["question_no"], "chat_turn": None if dd["chat_index"] is None else dd["chat_index"] + 1,
            "example_question": f.example_question,
            "closest_user_question": a.get("closest_user_question"),
            "why_missed": a.get("why_missed"), "better_question": a.get("better_question"),
        })
    result = {
        "revealed_at": sessions.now_iso(),
        "facts": rows,
        "overengineering_traps": sealed.overengineering_traps,
        "priority_confusion": sealed.priority_confusion,
        "good_solution_criteria": sealed.good_solution_criteria,
        "data_file_notes": sealed.data_file_notes,
        "ai_fit": sealed.ai_fit,
        "ai_fit_label": AI_FIT_LABEL.get(sealed.ai_fit, ""),
        "tech_requirement": sealed.tech_requirement,
        "themes": sealed.themes,
    }
    (d / "reveal.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if not (d / "retro.md").exists():
        (d / "retro.md").write_text(retro_template(session_id, result), encoding="utf-8")
    s.status = "revealed"
    sessions.save_session(s)
    return load(session_id)


def retro_template(session_id: str, rv: dict) -> str:
    problem = sessions.load_problem(session_id)
    rep = evaluate.load_report(session_id) or {}
    sc = rep.get("scores", {})
    tok = (rep.get("intent", {}).get("metrics", {}).get("tokens") or {})
    facts = rv["facts"]
    crit = [f for f in facts if f["importance"] == "critical"]
    missed = [f for f in facts if not f["discovered"]]
    missed_lines = "\n".join(
        f"  - {f['id']} ({f['type_label']}) {f['statement']}\n    → {f['better_question'] or f['example_question']}"
        for f in missed) or "  - 없음"
    actions = rep.get("intent", {}).get("next_actions", []) + ["", "", ""]
    fmt = lambda v: "-" if v is None else v  # noqa: E731
    return f"""# 모의고사 회고 ({datetime.now():%Y-%m-%d} / {problem.title})
## 결과
- 기술 {fmt(sc.get('technical'))} | 의도 {fmt(sc.get('intent'))} | 인지 {fmt(sc.get('cognitive'))} | 토큰 {fmt(tok.get('billable', tok.get('total')))}
- 숨은 사실 발견: {sum(f['discovered'] for f in facts)}/{len(facts)} (critical {sum(f['discovered'] for f in crit)}/{len(crit)})
## 인터뷰
- 놓친 요구사항과 했어야 할 질문:
{missed_lines}
## AI 협업
- 가장 효과적이었던 프롬프트:
- AI에 끌려간 순간:
## 시간
- 계획 대비 늦어진 단계와 원인:
## 다음에 바꿀 것 3가지
1. {actions[0]}
2. {actions[1]}
3. {actions[2]}
"""


def load(session_id: str) -> dict:
    d = sessions.session_dir(session_id)
    rv = json.loads((d / "reveal.json").read_text(encoding="utf-8"))
    rv["retro_md"] = (d / "retro.md").read_text(encoding="utf-8")
    return rv


def save_retro(session_id: str, text: str) -> None:
    (sessions.session_dir(session_id) / "retro.md").write_text(text, encoding="utf-8")
