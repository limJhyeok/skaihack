"""3축 평가 리포트 (5.9). 자동 실행 → 정량 지표 → LLM 평가(기술·의도·문서-코드 일치, 병렬) → 요약 → report.json."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import codectx, llm, metrics, prompts, rehearsal, runner, sessions
from .config import settings
from .schemas import ConsistencyEval, IntentEval, ReportSummary, Sealed, TechnicalEval

TECH_ITEMS = [("maintainability", "코드 유지보수성", 20), ("requirement_fulfillment", "요구사항 충족도", 20),
              ("functional_correctness", "기능적 정확성", 20), ("practical_quality_risk", "실무적 품질 위험", 20),
              ("deliverable_completeness", "제출물 완성도", 20)]
INTENT_ITEMS = [("output_goal", "Output 목표 구체화", 17), ("reusable_system", "재사용 가능한 작업 체계 구축", 17),
                ("intent_context", "의도 갱신 및 맥락 유지", 17), ("verification_loop", "검증 루프 설계 및 통제", 17),
                ("problem_constraints", "문제 상황 및 제약 정의", 16), ("decomposition_delegation", "작업 분해·위임 설계", 16)]
COG_ITEMS = [("behavior_risk", "AI 산출물 동작 및 리스크 예측"), ("structure", "AI 산출물 구조 이해"),
             ("decision_rationale", "AI 의사결정 및 근거 이해")]
RUN_FAIL_CAP = 6  # 앱이 뜨지 않으면 기능적 정확성 상한
CONSISTENCY_DOCS = ["RISKS.md", "DECISIONS.md", "ACCEPTANCE.md", "README.md"]


class EvaluationError(RuntimeError):
    pass


def hms(sec: int | None) -> str:
    if sec is None:
        return "?"
    sign = "-" if sec < 0 else ""
    sec = abs(int(sec))
    return f"{sign}{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


# ---------- 입력 텍스트 ----------

def facts_text(sealed: Sealed, disc: dict) -> str:
    lines = []
    for f in sealed.facts:
        d = disc[f.id]
        state = f"발견(질문 #{d['question_no']})" if d["discovered"] else ("부분 언급" if d["partial"] else "미발견")
        lines.append(f"- [{f.id}|{f.importance}|{f.type}] {f.statement} → {state}")
    return "\n".join(lines)


def chat_text(chat: list[dict], with_judgment: bool) -> str:
    lines = []
    for i, r in enumerate(chat, 1):
        line = f"#{i} (+{hms(r.get('elapsed_sec'))}) [{r['role']}] {r['text']}"
        q = r.get("question_quality")
        if with_judgment and r["role"] == "persona" and q:
            tags = [f"공개: {','.join(r.get('disclosed_fact_ids') or []) or '없음'}", f"질문 구체성 {q['specificity']}"]
            tags += [name for flag, name in (("is_dump_request", "덤프 요청"), ("is_confirmation", "이해 확인"),
                                             ("is_scope_proposal", "범위 제안"), ("scope_agreed", "범위 합의")) if q.get(flag)]
            line += f"  {{{'; '.join(tags)}}}"
        lines.append(line)
    return "\n".join(lines) or "(대화 없음)"


def prompts_text(process: dict) -> str:
    lines = []
    for si, t in enumerate(process.get("agent_traces", []), 1):
        lines.append(f"--- 세션 S{si} ({t['tool']}, 시작 +{hms(t.get('elapsed_start_sec'))}, 토큰 {(t.get('token_usage') or {}).get('billable', '?')})")
        for p in t["user_prompts"]:
            text = p["text"] if len(p["text"]) <= 1500 else p["text"][:1500] + "…(생략)"
            lines.append(f"S{si} +{hms(p.get('elapsed_sec'))}: {text}")
    return "\n".join(lines) or f"({process.get('agent_status', '수집 안 됨')})"


def commits_text(process: dict) -> str:
    return "\n".join(f"{c['hash']} +{hms(c.get('elapsed_sec'))} {c['subject']}\n{c.get('stat', '')}"
                     for c in process.get("commits", [])) or "(커밋 없음)"


def docs_text(docs: dict, names: list[str] | None = None) -> str:
    keys = names if names is not None else list(docs)
    return "\n\n".join(f"## {k}\n{docs[k]}" for k in keys if k in docs) or "(문서 없음)"


# ---------- 항목 정리 ----------

def normalize(items: list, spec: list[tuple]) -> list[dict]:
    by_key = {i.key: i for i in items}
    out = []
    for key, label, mx in spec:
        it = by_key.get(key)
        if it is None:
            out.append({"key": key, "label": label, "max": mx, "verdict": "미충족", "score": 0,
                        "rationale": "평가 응답에 이 항목이 없음", "evidence": [], "suggestion": ""})
            continue
        out.append({"key": key, "label": label, "max": mx, "verdict": it.verdict,
                    "score": max(0, min(mx, it.score)), "rationale": it.rationale,
                    "evidence": it.evidence, "suggestion": it.suggestion})
    return out


def apply_run_cap(items: list[dict], run: dict) -> None:
    if run.get("started"):
        return
    fc = next(i for i in items if i["key"] == "functional_correctness")
    fc["verdict"] = "미충족"
    fc["score"] = min(fc["score"], RUN_FAIL_CAP)
    reason = run.get("error") or "앱이 실행되지 않음"
    fc["rationale"] = f"[자동 실행 실패: {reason}] " + fc["rationale"]
    fc["evidence"] = ["run.log (리포트 하단 실행 로그)"] + fc["evidence"]


def cognitive_items(r: dict | None) -> tuple[list[dict], int | None]:
    if not r or r.get("status") != "graded":
        return [], None
    items = []
    for key, label in COG_ITEMS:
        cat = r["scores"]["by_category"][key]
        qs = [q for q in r["questions"] if q["category"] == key]
        score = cat["score"]
        verdict = "미충족" if score is None else ("충족" if score >= 80 else "부분충족" if score >= 40 else "미충족")
        items.append({
            "key": key, "label": label, "max": 100, "verdict": verdict, "score": score,
            "rationale": "해당 문항 없음" if not qs else " / ".join(
                f"{q['id']}({q['type']}) {q['grade']['verdict']}: {q['grade']['comment']}" for q in qs),
            "evidence": [c for q in qs for c in q["grade"]["revisit"]][:6],
            "suggestion": next((q["grade"]["comment"] for q in qs if q["grade"]["verdict"] != "충족"), ""),
        })
    return items, r["scores"]["total"]


# ---------- 타임라인 ----------

def timeline(session, chat: list[dict], process: dict, events: list[dict]) -> dict:
    ev = []
    qno = 0
    for i, r in enumerate(chat, 1):
        if r["role"] == "user":
            qno += 1
            ev.append({"lane": "chat", "t": r["elapsed_sec"], "label": f"질문 #{qno}: {r['text'][:80]}"})
        elif r.get("disclosed_fact_ids"):
            ev.append({"lane": "fact", "t": r["elapsed_sec"], "label": f"사실 {len(r['disclosed_fact_ids'])}개 공개 (채팅 #{i})"})
    for si, t in enumerate(process.get("agent_traces", []), 1):
        ev.append({"lane": "session", "t": t.get("elapsed_start_sec"), "label": f"에이전트 세션 S{si} 시작"})
        for p in t["user_prompts"]:
            ev.append({"lane": "prompt", "t": p.get("elapsed_sec"), "label": f"S{si}: {p['text'][:80]}"})
    for r in metrics.verification_runs(process.get("agent_traces", []), [])["timeline"]:
        mark = {True: "통과", False: "실패", None: "결과 모름"}[r["ok"]]
        ev.append({"lane": "verify", "t": r["elapsed_sec"], "ok": r["ok"], "label": f"S{r['session']} [{mark}] {r['cmd'][:80]}"})
    for c in process.get("commits", []):
        ev.append({"lane": "commit", "t": c.get("elapsed_sec"), "label": f"{c['hash'][:7]} {c['subject'][:60]}"})
    for e in events:
        if e["type"] == "alert":
            ev.append({"lane": "alert", "t": e["elapsed_sec"], "label": f"{e['at_min']}분 단계 알림"})
        elif e["type"] in ("pause", "resume", "submit"):
            ev.append({"lane": "alert", "t": e["elapsed_sec"], "label": {"pause": "일시정지", "resume": "재개", "submit": "제출"}[e["type"]]})
    ev = [e for e in ev if e["t"] is not None]
    end = max([session.duration_min * 60] + [e["t"] for e in ev])
    return {"duration_sec": end, "events": sorted(ev, key=lambda e: e["t"])}


# ---------- 실행 ----------

def evaluate(session_id: str) -> dict:
    s = sessions.load_session(session_id)
    d = sessions.session_dir(session_id)
    if s.status not in ("rehearsed", "evaluated", "revealed"):
        raise EvaluationError("설명 리허설을 마친 세션만 평가할 수 있습니다.")
    problem, sealed = sessions.load_problem(session_id), sessions.load_sealed(session_id)
    chat = sessions.read_jsonl(session_id, "chat.jsonl")
    events = sessions.read_jsonl(session_id, "events.jsonl")
    process = json.loads((d / "process.json").read_text(encoding="utf-8"))
    submission = json.loads((d / "submission.json").read_text(encoding="utf-8"))
    reh = rehearsal.load(session_id) if rehearsal.exists(session_id) else None

    # 1) 자동 실행 (포트 사용 중이면 PortBusy가 그대로 올라감)
    run = runner.run_app(d / "snapshot.tar.gz", s.run_command or "", s.app_port or 8000, d)
    (d / "run_result.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2) 정량 지표
    m = metrics.compute(readme=problem.readme_markdown, sealed=sealed, chat=chat, process=process,
                        workspace=Path(s.workspace_path), duration_sec=s.duration_min * 60)
    disc = metrics.discovery(sealed, chat)

    # 3) LLM 평가 (병렬)
    ctx = codectx.build(codectx.extract_snapshot(d / "snapshot.tar.gz", d / "snapshot"))
    docs = process.get("docs", {})
    agreed_turns = [f"#{i} [{r['role']}] {r['text']}" for i, r in enumerate(chat, 1)
                    if (r.get("question_quality") or {}).get("scope_agreed")
                    or (r["role"] == "user" and i < len(chat) and (chat[i].get("question_quality") or {}).get("is_scope_proposal"))]
    run_for_llm = {k: v for k, v in run.items() if k != "screenshot"}
    tech_prompt = prompts.load(
        "eval_technical", readme=problem.readme_markdown, facts_with_discovery=facts_text(sealed, disc),
        agreed_scope=docs_text(docs, ["SCOPE.md", "ACCEPTANCE.md"]) + "\n\n[채팅 속 범위 제안·합의]\n" + ("\n".join(agreed_turns) or "(없음)"),
        traps="\n".join(f"- {t}" for t in sealed.overengineering_traps), file_tree=ctx.tree_text(),
        key_files=ctx.key_files_text(), run_result=json.dumps(run_for_llm, ensure_ascii=False, indent=1),
        secret_scan=json.dumps(submission.get("secret_findings", []), ensure_ascii=False))
    intent_prompt = prompts.load(
        "eval_intent", readme=problem.readme_markdown, facts_with_discovery=facts_text(sealed, disc),
        duration_text=prompts.duration_text(s.duration_min), chat=chat_text(chat, with_judgment=True), agent_prompts=prompts_text(process),
        commits=commits_text(process), metrics=json.dumps(m, ensure_ascii=False, indent=1), docs=docs_text(docs))
    cons_docs = dict(docs)
    readme_ws = ctx.read("README.md")
    if readme_ws and readme_ws.strip() != problem.readme_markdown.strip():
        cons_docs["README.md"] = readme_ws  # 응시자가 고친 README만 검사 대상
    else:
        cons_docs.pop("README.md", None)
    cons_prompt = prompts.load("doc_code_consistency", docs=docs_text(cons_docs, CONSISTENCY_DOCS),
                               code=ctx.tree_text() + "\n\n" + ctx.key_files_text(), commits=commits_text(process))

    def call(system, schema, purpose):
        return llm.parse(model=settings.model_strong, system=system, user="평가하라.", schema=schema, purpose=purpose)

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_tech = pool.submit(call, tech_prompt, TechnicalEval, "eval_technical")
        f_int = pool.submit(call, intent_prompt, IntentEval, "eval_intent")
        f_con = pool.submit(call, cons_prompt, ConsistencyEval, "doc_code_consistency") \
            if any(k in cons_docs for k in CONSISTENCY_DOCS) else None
        tech, intent = f_tech.result(), f_int.result()
        cons = f_con.result() if f_con else ConsistencyEval(claims=[])

    tech_items = normalize(tech.items, TECH_ITEMS)
    apply_run_cap(tech_items, run)
    intent_items = normalize(intent.items, INTENT_ITEMS)
    cog_items, cog_total = cognitive_items(reh)
    comm = intent.communication
    scores = {
        "technical": sum(i["score"] for i in tech_items),
        "intent": sum(i["score"] for i in intent_items),
        "cognitive": cog_total,
        "communication": max(0, min(100, comm.score)),
    }

    # 4) 요약 (빠른 모델 1회)
    brief = [f"[{axis}] {i['label']}: {i['verdict']} ({i['score']}/{i['max']})"
             for axis, items in (("기술", tech_items), ("의도", intent_items), ("인지", cog_items)) for i in items]
    summary = llm.parse(model=settings.model_fast, system=prompts.load(
        "report_summary", scores=json.dumps(scores, ensure_ascii=False), items="\n".join(brief)),
        user="요약하라.", schema=ReportSummary, purpose="report_summary")

    report = {
        "generated_at": sessions.now_iso(),
        "scores": scores,
        "summary": summary.model_dump(),
        "technical": {"items": tech_items, "strengths": tech.strengths, "improvements": tech.improvements,
                      "undiscovered_note": tech.undiscovered_note, "run": run,
                      "secret_findings": submission.get("secret_findings", [])},
        "intent": {"items": intent_items, "cost_note": intent.cost_note, "best_moment": intent.best_moment,
                   "worst_moment": intent.worst_moment, "next_actions": intent.next_actions[:3], "metrics": m},
        "cognitive": {"items": cog_items, "total": cog_total},
        "communication": {**comm.model_dump(), "score": scores["communication"]},
        "consistency": {"claims": [c.model_dump() for c in cons.claims],
                        "mismatches": sum(c.verdict in ("inconsistent", "partially") for c in cons.claims)},
        "timeline": timeline(s, chat, process, events),
    }
    (d / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if s.status == "rehearsed":
        s.status = "evaluated"
        sessions.save_session(s)
    return report


def load_report(session_id: str) -> dict | None:
    p = sessions.session_dir(session_id) / "report.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
