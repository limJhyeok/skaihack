"""기록 대시보드 (5.12): 세션 목록, 축별 점수 추이, 사실 유형별 발견률과 습관 제안.

사실 유형 통계는 숨은 사실이 공개된(revealed) 세션만 집계한다. 공개 전 세션의 유형 구성은 봉인 정보이기 때문.
"""
from . import evaluate, metrics, sessions

TYPES = [("ask_to_reveal", "물어야 나오는 요구"), ("conflict", "자료와 충돌"), ("confirm_only", "확인만 해 주는 것"),
         ("constraint", "제약·금지"), ("detail", "현장 디테일")]
TIPS = {
    "ask_to_reveal": "업무 흐름을 단계별로 쪼개 \"그다음엔 뭘 하세요?\"처럼 이어서 물으세요.",
    "conflict": "자료에 적힌 숫자·기준을 그대로 인용하며 \"지금도 이 기준인가요?\"라고 확인하세요.",
    "confirm_only": "이해한 내용을 정리해 \"이렇게 이해했는데 맞나요?\"로 확인받는 질문을 하세요.",
    "constraint": "제약·금지 사항 질문을 습관화하세요: 저장해도 되는지, 외부로 보내도 되는지, 하면 안 되는 것은 무엇인지.",
    "detail": "\"최근에 실제로 있었던 일\"처럼 구체적인 과거 사례를 물으세요.",
    "critical": "담당자의 핵심 판단 기준(\"가장 먼저 보는 것\", \"좋은 결과의 기준\")을 초반에 물으세요.",
}
# 발견률이 같을 때 먼저 보여줄 순서 (실전 감점 위험이 큰 것부터)
TIP_PRIORITY = ["constraint", "conflict", "critical", "ask_to_reveal", "detail", "confirm_only"]
LOW_RATE = 0.5
MIN_TOTAL = 2


def _rate(found: int, total: int) -> float | None:
    return round(found / total, 2) if total else None


def build() -> dict:
    rows, trend = [], []
    type_counts = {k: [0, 0] for k, _ in TYPES}
    critical = [0, 0]
    revealed = 0
    for s in sorted(sessions.list_sessions(), key=lambda x: x.started_at or x.session_id):
        p = sessions.load_problem(s.session_id)
        rep = evaluate.load_report(s.session_id)
        scores = rep["scores"] if rep else {}
        m = rep["intent"]["metrics"] if rep else {}
        disc_all = (m.get("interview") or {}).get("discovery", {}).get("all") if m else None
        tok = (m.get("tokens") or {}) if m else {}
        row = {
            "session_id": s.session_id, "date": (s.started_at or "")[:10] or None, "title": p.title,
            "domain": p.domain, "archetype": p.archetype, "difficulty": p.difficulty, "status": s.status,
            "technical": scores.get("technical"), "intent": scores.get("intent"), "cognitive": scores.get("cognitive"),
            "discovery": disc_all, "tokens": tok.get("billable", tok.get("total")),
        }
        rows.append(row)
        if rep:
            trend.append({k: row[k] for k in ("session_id", "date", "domain", "difficulty", "technical", "intent", "cognitive")})
        if s.status == "revealed":
            revealed += 1
            sealed = sessions.load_sealed(s.session_id)
            disc = metrics.discovery(sealed, sessions.read_jsonl(s.session_id, "chat.jsonl"))
            for f in sealed.facts:
                if f.type not in type_counts:  # buried: 대화 발견 대상이 아님
                    continue
                type_counts[f.type][1] += 1
                type_counts[f.type][0] += disc[f.id]["discovered"]
                if f.importance == "critical":
                    critical[1] += 1
                    critical[0] += disc[f.id]["discovered"]

    by_type = [{"type": k, "label": label, "found": type_counts[k][0], "total": type_counts[k][1],
                "rate": _rate(*type_counts[k])} for k, label in TYPES]
    by_type.append({"type": "critical", "label": "중요도 critical (유형 무관)", "found": critical[0],
                    "total": critical[1], "rate": _rate(*critical)})
    tips = [{"type": t["type"], "label": t["label"], "rate": t["rate"], "tip": TIPS[t["type"]]}
            for t in by_type if t["total"] >= MIN_TOTAL and t["rate"] is not None and t["rate"] < LOW_RATE]
    tips.sort(key=lambda t: (t["rate"], TIP_PRIORITY.index(t["type"])))
    return {"sessions": rows[::-1], "trend": trend, "revealed_sessions": revealed,
            "fact_types": by_type, "tips": tips}
