"""D. 변경 대응 드릴 (docs/flow_drills_design.md 4절).

시험 중간에 현업이 정책을 바꾸는 공지를 받고, 고쳐야 하는 요구사항(정책)·성공 기준 예시(+ 새 기대값)·코드 위치를 고른다.

정책 변경은 묶음마다 한 번 LLM으로 출제한다 (공지 + 바뀐 정책 문구 + 정답 구현에 대한 치환). 정답은 실행으로 정한다:
- 영향받는 예시 = 치환을 적용한 구현에서 값이 달라진 정책 예시. 새 기대값 = 그 실행 결과.
- 영향받는 요구사항 = 그 예시들의 정책 + 출제자가 문구가 바뀐다고 적은 정책.
- 고칠 코드 위치 = 치환으로 바뀐 줄이 속한 최상위 정의.

저장: 변경은 bundle.json의 changes, 풀이는 data/drills/responses/<response_id>/response.json
"""
import re
from datetime import datetime

from . import cards, llm, policy, prompts, sessions
from .config import settings
from .schemas import PolicyChangesOut

MAX_ATTEMPTS = 3
CHANGES = (3, 4)
MIN_KEEP = 2
MAX_SHARE = 0.5          # 전체 예시 중 바뀌는 비율 상한
SOFT_SEC = 300
NOTICE_BANNED = ["결함", "버그", "정답 구현"]


class ChangeError(RuntimeError):
    pass


# ---------- 출제 ----------

def _policies_text(bundle: dict) -> str:
    out = []
    for p in bundle["policies"]:
        out.append(f"- {p['id']} [{policy.KIND_LABEL[p['kind']]}] {p['title']}: {p['statement']} (위치: {p['location']})")
        for i, ex in enumerate(p["examples"], 1):
            args = ", ".join(f"{a['name']}={a['value']}" for a in ex["args"])
            out.append(f"    {p['id']}.{i} {ex['func']}({args}) → {ex['expected']}  # {ex['note']}")
    return "\n".join(out)


def check_changes(bundle: dict, out: PolicyChangesOut) -> tuple[list[dict], list[str]]:
    """변경마다 독립 검사. 반환: (통과한 변경과 실행으로 정한 정답, 실패 사유)."""
    ref = bundle["reference_code"]
    examples = policy.policy_examples(bundle)
    cases = [e["case"] for e in examples]
    pids = {p["id"] for p in bundle["policies"]}
    names = [f["name"] for f in bundle["functions"]] + [c["name"] for c in policy.chunks(ref) if c["kind"] != "other"]
    defect_codes = set(policy.variants(bundle).values())
    errs, codes = [], {}
    items = {}
    for ch in out.changes:
        e0 = len(errs)
        text = ch.title + "\n" + ch.notice
        if not ch.notice.strip():
            errs.append(f"{ch.id}: notice가 비어 있음")
        if bad := [n for n in names if n in text] + [t for t in NOTICE_BANNED + policy.DOC_BANNED if t in text]:
            errs.append(f"{ch.id}: 공지에 쓰면 안 되는 말 {', '.join(sorted(set(bad)))}")
        if re.search(r"\bP\d+\b", text):
            errs.append(f"{ch.id}: 공지에 정책 id가 나옴")
        if not ch.changed_policies or {c.policy_id for c in ch.changed_policies} - pids:
            errs.append(f"{ch.id}: changed_policies가 비었거나 없는 정책 id가 있음")
        try:
            code = policy.apply_edits(ref, ch.edits)
        except policy.PolicyError as e:
            errs.append(f"{ch.id}: {e}")
            continue
        if code == ref:
            errs.append(f"{ch.id}: 치환 후에도 정답 구현과 같음")
        elif code in defect_codes:
            errs.append(f"{ch.id}: 이미 있는 결함과 같은 변경")
        errs += policy.static_errors(code, ch.id)
        if len(errs) == e0:
            codes[ch.id] = code
            items[ch.id] = ch
    runs = policy.run_variants({"ref": ref, **codes}, cases)
    valid = []
    for cid, code in codes.items():
        ch = items[cid]
        res = runs[cid]
        broken = [e["eid"] for e in examples if not res[e["eid"]]["ok"]]
        if broken:
            errs.append(f"{cid}: 고친 구현이 예시 {', '.join(broken[:5])}에서 실행 오류 ({res[broken[0]].get('error', '')})")
            continue
        changed = [e for e in examples if res[e["eid"]]["value"] != runs["ref"][e["eid"]]["value"]]
        if not changed:
            errs.append(f"{cid}: 고친 구현에서 값이 달라지는 정책 예시가 없음 (변경이 예시로 드러나야 함)")
            continue
        if len(changed) > MAX_SHARE * len(examples):
            errs.append(f"{cid}: 예시 {len(changed)}/{len(examples)}개가 바뀜 (너무 넓음, 절반 이하로)")
            continue
        declared = [c.policy_id for c in ch.changed_policies]
        by_value = {e["policy_id"] for e in changed}
        hits = policy.changed_lines(ref, code)
        order = [p["id"] for p in bundle["policies"]]
        valid.append({
            "id": cid, "title": ch.title, "notice": ch.notice,
            "changed_policies": [c.model_dump() for c in ch.changed_policies],
            "edits": [e.model_dump() for e in ch.edits], "code": code, "hits": hits,
            "chunks": policy.touched_chunks(code, hits),
            "examples": [{"eid": e["eid"], "policy_id": e["policy_id"], "old": runs["ref"][e["eid"]]["value"],
                          "new": res[e["eid"]]["value"]} for e in changed],
            "policies": sorted(set(declared) | by_value, key=order.index),
        })
    return valid, errs


def generate_changes(bundle: dict) -> tuple[list[dict], list[dict]]:
    """변경을 출제·검사한다. 시도마다 통과한 변경을 모으고 (같은 코드는 하나만), CHANGES[0]개 모이면 멈춘다."""
    kept: dict[str, dict] = {}
    feedback, attempts = "", []
    defects = "\n".join(f"- {d['id']} ({d['policy_id']}): {d['summary']}" for d in bundle["defects"])
    for i in range(MAX_ATTEMPTS):
        system = prompts.load(
            "policy_changes", spec_doc=bundle["spec_doc"], policies=_policies_text(bundle),
            reference_code=bundle["reference_code"], defects=defects, change_min=CHANGES[0], change_max=CHANGES[1],
            allowed_imports=", ".join(sorted(policy.ALLOWED_IMPORTS - {"__future__"})), retry_feedback=feedback)
        out = llm.parse(model=settings.model_strong, system=system, user="정책 변경을 만들어라.",
                        schema=PolicyChangesOut, purpose="policy_changes")
        valid, errs = check_changes(bundle, out)
        for v in valid:
            if v["code"] not in {k["code"] for k in kept.values()}:
                kept[v["code"]] = v
        attempts.append({"attempt": i + 1, "valid": [v["title"] for v in valid], "errors": errs})
        if len(kept) >= CHANGES[0]:
            break
        feedback = ("\n[이전 출력 검사 결과 — 이번에는 반드시 고칠 것]\n"
                    + "".join(f"- {e}\n" for e in errs[:20])
                    + ("이미 통과한 변경(같은 것을 다시 내지 말고 다른 변경을 낼 것): "
                       + "; ".join(v["title"] for v in kept.values()) + "\n" if kept else ""))
    if len(kept) < MIN_KEEP:
        raise ChangeError("정책 변경 출제 실패 (3회): " + " / ".join(attempts[-1]["errors"])[:800])
    changes = list(kept.values())[:CHANGES[1]]
    for i, c in enumerate(changes, 1):
        c["id"] = f"C{i}"
    return changes, attempts


def ensure_changes(bundle_id: str) -> dict:
    bundle = policy.load(bundle_id)
    if not bundle.get("changes"):
        changes, attempts = generate_changes(bundle)
        bundle = policy.load(bundle_id)     # 출제하는 동안 바뀌었을 수 있음
        bundle["changes"] = changes
        bundle["changes_meta"] = {"created_at": sessions.now_iso(), "attempts": attempts}
        cards._write("bundles", bundle_id, bundle)
    return bundle


# ---------- 진행 ----------

def create(bundle_id: str) -> dict:
    bundle = ensure_changes(bundle_id)
    rid = cards._new_id("k")
    resp = {"response_id": rid, "bundle_id": bundle_id, "created_at": sessions.now_iso(),
            "cards": [{"n": i, "change_id": c["id"], "served_at": None, "answer": None, "result": None}
                      for i, c in enumerate(bundle["changes"], 1)]}
    cards._write("responses", rid, resp)
    return resp


def load(response_id: str) -> dict:
    return cards._read("responses", response_id)


def _card(resp: dict, n: int) -> dict:
    if not 1 <= n <= len(resp["cards"]):
        raise ChangeError("없는 카드입니다.")
    return resp["cards"][n - 1]


def _change(bundle: dict, c: dict) -> dict:
    return next(x for x in bundle["changes"] if x["id"] == c["change_id"])


def _next(resp: dict) -> dict | None:
    return next((c for c in resp["cards"] if c["answer"] is None), None)


def _elapsed(c: dict) -> int:
    if not c["served_at"]:
        return 0
    end = datetime.fromisoformat(c["answer"]["at"]) if c["answer"] else datetime.now().astimezone()
    return int((end - datetime.fromisoformat(c["served_at"])).total_seconds())


def _material(bundle: dict) -> dict:
    """응시자가 지금까지 쌓은 것: 요구사항(정책), 성공 기준 예시, 코드."""
    fns = {f["name"]: f for f in bundle["functions"]}
    ref = bundle["reference_code"]
    return {
        "policies": [{k: v for k, v in policy.policy_ref(p).items() if k != "doc_quote"} for p in bundle["policies"]],
        "examples": [{"eid": e["eid"], "policy_id": e["policy_id"], "func": e["func"], "call": e["call"],
                      "expected": e["expected"], "returns": fns[e["func"]]["returns"]}
                     for e in policy.policy_examples(bundle)],
        "chunks": [{"name": c["name"], "kind": c["kind"], "start": c["start"], "end": c["end"]}
                   for c in policy.chunks(ref)],
        "code": [{"n": i, "text": t, "hit": False} for i, t in enumerate(ref.splitlines(), 1)],
    }


def question(resp: dict, bundle: dict, c: dict) -> dict:
    ch = _change(bundle, c)
    return {"n": c["n"], "total": len(resp["cards"]), "title": ch["title"], "notice": ch["notice"],
            "elapsed_sec": _elapsed(c), "soft_sec": SOFT_SEC, **_material(bundle)}


def answer(response_id: str, n: int, policies: list[str], examples: dict[str, str], chunks: list[str]) -> dict:
    resp = load(response_id)
    c = _card(resp, n)
    if c["answer"] is not None:
        raise ChangeError("이미 답한 카드입니다.")
    bundle = policy.load(resp["bundle_id"])
    pids = {p["id"] for p in bundle["policies"]}
    eids = {e["eid"] for e in policy.policy_examples(bundle)}
    names = {x["name"] for x in policy.chunks(bundle["reference_code"])}
    if set(policies) - pids or set(examples) - eids or set(chunks) - names:
        raise ChangeError("없는 정책·예시·코드 위치가 있습니다.")
    c["served_at"] = c["served_at"] or sessions.now_iso()
    c["answer"] = {"policies": sorted(set(policies)), "examples": {k: str(v)[:200] for k, v in examples.items()},
                   "chunks": sorted(set(chunks)), "at": sessions.now_iso()}
    c["result"] = grade_card(bundle, _change(bundle, c), c["answer"])
    cards._write("responses", response_id, resp)
    return result(resp, bundle, c)


def _sets(truth: set, mine: set) -> dict:
    hit = truth & mine
    return {"hit": len(hit), "missed": len(truth - mine), "extra": len(mine - truth), "truth": len(truth),
            "recall": round(100 * len(hit) / len(truth)) if truth else None,
            "precision": round(100 * len(hit) / len(mine)) if mine else None,
            "exact": truth == mine}


def _readers(bundle: dict, ch: dict) -> list[str]:
    """바뀐 상수(기준표)를 읽기만 하는 정의. 골라도 감점하지 않는다 (고칠 곳은 아니지만 확인할 곳)."""
    r = policy.reach(bundle["reference_code"])
    consts = {c["name"] for c in policy.chunks(bundle["reference_code"])
              if c["name"] in ch["chunks"] and c["kind"] != "func"}
    return [n for n, deps in r.items() if n not in ch["chunks"] and deps & consts]


def grade_card(bundle: dict, ch: dict, a: dict) -> dict:
    exs = {e["eid"]: e for e in policy.policy_examples(bundle)}
    fns = {f["name"]: f for f in bundle["functions"]}
    truth_new = {x["eid"]: x["new"] for x in ch["examples"]}
    values = {}
    for eid, new in truth_new.items():
        if eid not in a["examples"]:
            continue
        try:
            values[eid] = policy.expected_of(fns[exs[eid]["func"]], a["examples"][eid]) == new
        except ValueError:
            values[eid] = False
    pol = _sets(set(ch["policies"]), set(a["policies"]))
    ex = _sets(set(truth_new), set(a["examples"]))
    code = _sets(set(ch["chunks"]), set(a["chunks"]) - set(_readers(bundle, ch)))
    vok = sum(values.values())
    return {"policies": pol, "examples": ex, "code": code, "values_ok": vok, "values": values,
            "all_correct": pol["exact"] and ex["exact"] and code["exact"] and vok == len(truth_new)}


def result(resp: dict, bundle: dict, c: dict) -> dict:
    ch = _change(bundle, c)
    a, r = c["answer"], c["result"]
    pols = {p["id"]: p for p in bundle["policies"]}
    exs = {e["eid"]: e for e in policy.policy_examples(bundle)}
    new_stmt = {x["policy_id"]: x["statement"] for x in ch["changed_policies"]}
    by_value = {x["policy_id"] for x in ch["examples"]}
    truth_ex = {x["eid"]: x for x in ch["examples"]}

    pol_rows = []
    for p in bundle["policies"]:
        t, m = p["id"] in ch["policies"], p["id"] in a["policies"]
        if t or m:
            pol_rows.append({**policy.policy_ref(p), "truth": t, "mine": m, "new_statement": new_stmt.get(p["id"]),
                             "why": ("예시 값이 바뀜" if p["id"] in by_value else "문구만 바뀜") if t else "바뀌지 않음",
                             "ripple": t and p["id"] not in new_stmt})
    ex_rows = []
    for e in exs.values():
        t, m = e["eid"] in truth_ex, e["eid"] in a["examples"]
        if t or m:
            ex_rows.append({"eid": e["eid"], "policy_id": e["policy_id"], "call": e["call"], "old": e["expected"],
                            "new": truth_ex[e["eid"]]["new"] if t else e["expected"], "mine": a["examples"].get(e["eid"]),
                            "truth": t, "chosen": m, "value_ok": r["values"].get(e["eid"])})
    readers = _readers(bundle, ch)
    code_rows = [{"name": x["name"], "truth": x["name"] in ch["chunks"], "mine": x["name"] in a["chunks"],
                  "reader": x["name"] in readers}
                 for x in policy.chunks(bundle["reference_code"])
                 if x["name"] in set(ch["chunks"]) | set(a["chunks"]) | set(readers)]
    return {"n": c["n"], "total": len(resp["cards"]), "title": ch["title"], "notice": ch["notice"],
            "elapsed_sec": _elapsed(c), "result": {k: v for k, v in r.items() if k != "values"},
            "policies": pol_rows, "examples": ex_rows, "code_rows": code_rows,
            "new_code": policy.snippet(ch["code"], ch["chunks"], ch["hits"]),
            "old_code": policy.snippet(bundle["reference_code"], ch["chunks"]),
            "unchanged_examples": len(exs) - len(truth_ex),
            "done": _next(resp) is None}


def summary(resp: dict) -> dict:
    ans = [c for c in resp["cards"] if c["answer"]]
    avg = lambda key, f: (round(sum(c["result"][key][f] or 0 for c in ans) / len(ans)) if ans else None)
    truth_vals = sum(c["result"]["examples"]["truth"] for c in ans)
    return {"cards": len(resp["cards"]), "answered": len(ans),
            "all_correct": sum(1 for c in ans if c["result"]["all_correct"]),
            "policy_recall": avg("policies", "recall"), "policy_precision": avg("policies", "precision"),
            "example_recall": avg("examples", "recall"), "example_precision": avg("examples", "precision"),
            "code_recall": avg("code", "recall"), "code_precision": avg("code", "precision"),
            "values_ok": sum(c["result"]["values_ok"] for c in ans), "values_total": truth_vals,
            "avg_sec": round(sum(_elapsed(c) for c in ans) / len(ans)) if ans else None}


def view(response_id: str) -> dict:
    resp = load(response_id)
    bundle = policy.load(resp["bundle_id"])
    nxt = _next(resp)
    if nxt and not nxt["served_at"]:
        nxt["served_at"] = sessions.now_iso()
        cards._write("responses", response_id, resp)
    titles = {x["id"]: x["title"] for x in bundle["changes"]}
    return {"response_id": response_id, "bundle_id": resp["bundle_id"], "title": bundle["title"],
            "spec_doc": bundle["spec_doc"], "question": question(resp, bundle, nxt) if nxt else None,
            "summary": summary(resp),
            "items": [{"n": c["n"], "title": titles[c["change_id"]], "answered": c["answer"] is not None,
                       "all_correct": c["result"]["all_correct"] if c["result"] else None} for c in resp["cards"]]}


def card_result(response_id: str, n: int) -> dict:
    resp = load(response_id)
    c = _card(resp, n)
    if c["answer"] is None:
        raise ChangeError("아직 답하지 않은 카드입니다.")
    return result(resp, policy.load(resp["bundle_id"]), c)


def overview() -> dict:
    resps = cards._all("responses")
    done = policy.drills_done()
    bundles = []
    for b in reversed(policy.all_bundles()):
        mine = [r for r in resps if r["bundle_id"] == b["bundle_id"]]
        bundles.append({**policy.summary(b), "changes": len(b.get("changes") or []), "responses": len(mine),
                        "drills": sorted(done.get(b["bundle_id"], set()) - {"D"})})
    titles = {b["bundle_id"]: b["title"] for b in bundles}
    rows = []
    for r in reversed(resps):
        s = summary(r)
        rows.append({"response_id": r["response_id"], "bundle_id": r["bundle_id"],
                     "title": titles.get(r["bundle_id"], "-"), "created_at": r["created_at"],
                     "done": s["answered"] == s["cards"], **s})
    return {"bundles": bundles, "responses": rows}
