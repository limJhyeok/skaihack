"""C. AI 변경 검토 드릴 (docs/flow_drills_design.md 4절).

"에이전트가 방금 작성한 코드"(정답 구현 또는 결함 버전의 한 정의)와 그 코드에 걸린 요구사항(정책)을 보여 준다.
응시자는 위반 여부를 고르고, 위반이면 어긴 정책과 줄을 고른다. 정답을 미리 알고 있으므로 즉시 판정한다 (LLM 없음).

- 결함 카드: 정답 구현에 결함 치환을 적용한 코드에서, 바뀐 줄이 속한 정의를 보여 준다. 정답 줄 = 바뀐 줄.
  인정하는 정책 = 결함의 정책 + 이 결함 버전에서 실제로 값이 달라진 예시의 정책(실행 관찰값).
- 정상 카드: 정답 구현의 함수 하나. 관련 정책 예시를 모두 통과한 코드다.
- 한 묶음 안에서 같은 정의는 한 번만 나온다 (같은 함수의 정상·결함 판을 나란히 보면 비교만으로 풀리므로).

저장: data/drills/reviews/<review_id>/review.json
"""
import random
from collections import Counter
from datetime import datetime

from . import cards, policy, sessions

P_CORRECT = 0.3          # 결함이 있는 정의를 정상 판으로 낼 확률
P_EXTRA_CORRECT = 0.5    # 결함이 없는 함수를 정상 카드로 더 낼 확률
SOFT_SEC = 180


class ReviewError(RuntimeError):
    pass


# ---------- 카드 만들기 ----------

def _display_names(code: str, touched: list[str]) -> list[str]:
    """보여 줄 정의: 바뀐 정의 + (상수가 바뀌었으면) 그 상수를 쓰는 함수 + 보이는 함수가 읽는 상수(기준표 맥락)."""
    chs = policy.chunks(code)
    kinds = {c["name"]: c["kind"] for c in chs}
    r = policy.reach(code)
    names = set(touched)
    consts = {n for n in touched if kinds.get(n) != "func"}
    if consts:
        names |= {n for n, deps in r.items() if kinds[n] == "func" and deps & consts}
    names |= {d for n in list(names) for d in r.get(n, ()) if kinds.get(d) != "func"}
    return [c["name"] for c in chs if c["name"] in names]


def _defect_unit(bundle: dict, d: dict) -> dict:
    ref = bundle["reference_code"]
    code = policy.apply_edits(ref, d["edits"])
    hits = policy.changed_lines(ref, code)
    touched = policy.touched_chunks(code, hits)
    return {"defect": d, "code": code, "hits": hits, "touched": touched, "names": _display_names(code, touched)}


def _funcs(code: str, names: list[str]) -> list[str]:
    return [c["name"] for c in policy.chunks(code) if c["name"] in names and c["kind"] == "func"]


def _defect_card(bundle: dict, u: dict, examples: dict) -> dict:
    d = u["defect"]
    related = policy.related_policies(bundle, set(u["touched"]))
    if d["policy_id"] not in related:
        related.append(d["policy_id"])
    related = [p["id"] for p in bundle["policies"] if p["id"] in related]
    kills = [e for e in (bundle.get("verification") or {}).get("kills", {}).get(d["id"], []) if e in examples]
    runs = policy.run_cases(u["code"], [examples[e]["case"] for e in kills])
    evidence = []
    for e in kills:
        ex, r = examples[e], runs.get(e, {"ok": False, "error": "실행 실패"})
        evidence.append({"eid": e, "policy_id": ex["policy_id"], "func": ex["func"], "call": ex["call"],
                         "expected": ex["expected"], "note": ex["note"],
                         "actual": r["value"] if r["ok"] else f"(오류) {r.get('error', '')}"})
    accepted = sorted({d["policy_id"]} | {x["policy_id"] for x in evidence if x["policy_id"] in related},
                      key=related.index)
    return {"kind": "defect", "defect_id": d["id"], "names": u["names"], "funcs": _funcs(u["code"], u["names"]),
            "touched": u["touched"],
            "policies": related,
            "code": policy.snippet(u["code"], u["names"]), "hits": u["hits"],
            "ref_code": policy.snippet(bundle["reference_code"], u["names"]),
            "accepted": accepted, "evidence": evidence}


def _correct_card(bundle: dict, name: str, examples: dict) -> dict:
    related = policy.related_policies(bundle, {name})
    r = policy.reach(bundle["reference_code"])
    passed = [e for e, ex in examples.items() if name in r.get(ex["func"], {ex["func"]})]
    names = _display_names(bundle["reference_code"], [name])
    return {"kind": "correct", "defect_id": None, "names": names, "funcs": [name], "touched": [name],
            "policies": related,
            "code": policy.snippet(bundle["reference_code"], names), "hits": [], "ref_code": [],
            "accepted": [], "evidence": [], "passed": len(passed)}


def build_cards(bundle: dict, rng: random.Random, used: Counter = Counter()) -> list[dict]:
    """정의마다 한 번만. 결함이 있는 정의는 P_CORRECT 확률로 정상 판을, 아니면 덜 본 결함 하나를 낸다."""
    examples = {e["eid"]: e for e in policy.policy_examples(bundle)}
    funcs = [c["name"] for c in policy.chunks(bundle["reference_code"]) if c["kind"] == "func"]
    units: dict[tuple, list[dict]] = {}
    for d in bundle["defects"]:
        u = _defect_unit(bundle, d)
        units.setdefault(tuple(u["touched"]), []).append(u)
    keys = list(units)
    rng.shuffle(keys)
    deck, taken = [], set()

    def pick_defect(key):
        cands = sorted(units[key], key=lambda x: (used[x["defect"]["id"]], rng.random()))
        return _defect_card(bundle, cands[0], examples)

    for key in keys:
        if taken & set(key):
            continue
        taken |= set(key)
        if len(key) == 1 and key[0] in funcs and rng.random() < P_CORRECT:
            deck.append(_correct_card(bundle, key[0], examples))
        else:
            deck.append(pick_defect(key))
    for f in funcs:
        if f not in taken and policy.related_policies(bundle, {f}) and rng.random() < P_EXTRA_CORRECT:
            deck.append(_correct_card(bundle, f, examples))
            taken.add(f)
    if keys and not any(c["kind"] == "defect" for c in deck):
        key = rng.choice(keys)
        deck = [c for c in deck if not set(c["touched"]) & set(key)] + [pick_defect(key)]
    rng.shuffle(deck)
    for i, c in enumerate(deck, 1):
        c.update({"n": i, "served_at": None, "answer": None, "result": None})
    return deck


# ---------- 진행 ----------

def create(bundle_id: str, seed: int | None = None) -> dict:
    bundle = policy.load(bundle_id)
    used = Counter(c["defect_id"] for r in cards._all("reviews") if r["bundle_id"] == bundle_id
                   for c in r["cards"] if c["defect_id"])
    rid = cards._new_id("v")
    rv = {"review_id": rid, "bundle_id": bundle_id, "created_at": sessions.now_iso(),
          "cards": build_cards(bundle, random.Random(seed), used)}
    cards._write("reviews", rid, rv)
    return rv


def load(review_id: str) -> dict:
    return cards._read("reviews", review_id)


def _card(rv: dict, n: int) -> dict:
    if not 1 <= n <= len(rv["cards"]):
        raise ReviewError("없는 카드입니다.")
    return rv["cards"][n - 1]


def _next(rv: dict) -> dict | None:
    return next((c for c in rv["cards"] if c["answer"] is None), None)


def _agent_note(c: dict) -> str:
    return f"에이전트: {', '.join(c['funcs'] or c['names'])} 구현을 마쳤습니다. 요구사항대로 작성했습니다."


def question(rv: dict, bundle: dict, c: dict) -> dict:
    pols = {p["id"]: p for p in bundle["policies"]}
    hidden = [{**s, "lines": [{"n": l["n"], "text": l["text"], "hit": False} for l in s["lines"]]} for s in c["code"]]
    return {"n": c["n"], "total": len(rv["cards"]), "agent_note": _agent_note(c), "code": hidden,
            "policies": [{k: v for k, v in policy.policy_ref(pols[p]).items() if k != "doc_quote"} for p in c["policies"]],
            "elapsed_sec": _elapsed(c), "soft_sec": SOFT_SEC}


def _elapsed(c: dict) -> int:
    if not c["served_at"]:
        return 0
    end = datetime.fromisoformat(c["answer"]["at"]) if c["answer"] else datetime.now().astimezone()
    return int((end - datetime.fromisoformat(c["served_at"])).total_seconds())


def answer(review_id: str, n: int, verdict: str, policy_id: str | None, line: int | None) -> dict:
    rv = load(review_id)
    c = _card(rv, n)
    if c["answer"] is not None:
        raise ReviewError("이미 답한 카드입니다.")
    if verdict not in ("ok", "violation"):
        raise ReviewError("판정은 ok 또는 violation입니다.")
    if verdict == "violation":
        if policy_id not in c["policies"]:
            raise ReviewError("위반한 요구사항을 고르세요.")
        shown = {l["n"] for s in c["code"] for l in s["lines"]}
        if line is not None and line not in shown:
            raise ReviewError("보이는 코드의 줄을 고르세요.")
    else:
        policy_id, line = None, None
    c["served_at"] = c["served_at"] or sessions.now_iso()
    c["answer"] = {"verdict": verdict, "policy_id": policy_id, "line": line, "at": sessions.now_iso()}
    c["result"] = grade_card(c)
    cards._write("reviews", review_id, rv)
    return result(rv, policy.load(rv["bundle_id"]), c)


def grade_card(c: dict) -> dict:
    a = c["answer"]
    if c["kind"] == "correct":
        ok = a["verdict"] == "ok"
        return {"verdict_ok": ok, "policy_ok": None, "line_ok": None, "all_correct": ok}
    v = a["verdict"] == "violation"
    p = v and a["policy_id"] in c["accepted"]
    ln = v and a["line"] in c["hits"]
    return {"verdict_ok": v, "policy_ok": p, "line_ok": ln, "all_correct": v and p and ln}


def result(rv: dict, bundle: dict, c: dict) -> dict:
    pols = {p["id"]: p for p in bundle["policies"]}
    d = next((x for x in bundle["defects"] if x["id"] == c["defect_id"]), None)
    return {"n": c["n"], "total": len(rv["cards"]), "kind": c["kind"], "agent_note": _agent_note(c),
            "answer": c["answer"], **c["result"], "elapsed_sec": _elapsed(c),
            "code": c["code"] if c["kind"] == "correct" else
            [{**s, "lines": [{**l, "hit": l["n"] in c["hits"]} for l in s["lines"]]} for s in c["code"]],
            "ref_code": c["ref_code"], "hits": c["hits"],
            "defect_summary": d["summary"] if d else None,
            "policy": policy.policy_ref(pols[d["policy_id"]]) if d else None,
            "accepted": [policy.policy_ref(pols[p]) for p in c["accepted"]],
            "mine_policy": policy.policy_ref(pols[c["answer"]["policy_id"]]) if c["answer"]["policy_id"] else None,
            "evidence": c["evidence"], "passed": c.get("passed"),
            "done": _next(rv) is None}


def summary(rv: dict) -> dict:
    ans = [c for c in rv["cards"] if c["answer"]]
    defect = [c for c in ans if c["kind"] == "defect"]
    caught = [c for c in defect if c["result"]["verdict_ok"]]
    correct = [c for c in ans if c["kind"] == "correct"]
    pct = lambda a, b: round(100 * a / b) if b else None
    return {"cards": len(rv["cards"]), "answered": len(ans),
            "all_correct": sum(1 for c in ans if c["result"]["all_correct"]),
            "defects": len(defect), "caught": len(caught), "catch_rate": pct(len(caught), len(defect)),
            "policy_ok": sum(1 for c in caught if c["result"]["policy_ok"]),
            "line_ok": sum(1 for c in caught if c["result"]["line_ok"]),
            "clean": len(correct), "false_alarm": sum(1 for c in correct if not c["result"]["verdict_ok"]),
            "avg_sec": round(sum(_elapsed(c) for c in ans) / len(ans)) if ans else None}


def view(review_id: str) -> dict:
    """다음 카드가 있으면 그 문제(처음 보는 순간부터 시간을 잰다), 없으면 요약."""
    rv = load(review_id)
    bundle = policy.load(rv["bundle_id"])
    nxt = _next(rv)
    if nxt and not nxt["served_at"]:
        nxt["served_at"] = sessions.now_iso()
        cards._write("reviews", review_id, rv)
    return {"review_id": review_id, "bundle_id": rv["bundle_id"], "title": bundle["title"],
            "spec_doc": bundle["spec_doc"], "question": question(rv, bundle, nxt) if nxt else None,
            "summary": summary(rv),
            "items": [{"n": c["n"], "kind": c["kind"] if c["answer"] else None, "names": c["names"],
                       "answered": c["answer"] is not None,
                       "all_correct": c["result"]["all_correct"] if c["result"] else None} for c in rv["cards"]]}


def card_result(review_id: str, n: int) -> dict:
    rv = load(review_id)
    c = _card(rv, n)
    if c["answer"] is None:
        raise ReviewError("아직 답하지 않은 카드입니다.")
    return result(rv, policy.load(rv["bundle_id"]), c)


def overview() -> dict:
    reviews = cards._all("reviews")
    done = policy.drills_done()
    bundles = []
    for b in reversed(policy.all_bundles()):
        mine = [r for r in reviews if r["bundle_id"] == b["bundle_id"]]
        seen = {c["defect_id"] for r in mine for c in r["cards"] if c["defect_id"] and c["answer"]}
        bundles.append({**policy.summary(b), "reviews": len(mine), "defects_seen": len(seen),
                        "drills": sorted(done.get(b["bundle_id"], set()) - {"C"})})
    titles = {b["bundle_id"]: b["title"] for b in bundles}
    rows = []
    for r in reversed(reviews):
        s = summary(r)
        rows.append({"review_id": r["review_id"], "bundle_id": r["bundle_id"], "title": titles.get(r["bundle_id"], "-"),
                     "created_at": r["created_at"], "done": s["answered"] == s["cards"], **s})
    return {"bundles": bundles, "reviews": rows}
