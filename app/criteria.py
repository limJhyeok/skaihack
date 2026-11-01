"""B. 성공 기준 작성 드릴 (docs/flow_drills_design.md 4절).

명세 문서와 함수 모양만 보고 정책을 확인할 예시(입력 → 기대 출력)를 적는다. 채점은 실행으로 한다 (LLM 없음).
- 정확도: 예시를 정답 구현에 넣어 기대값과 같은가 → 정책 이해.
- 검출률: 맞는 예시로 결함 버전을 몇 개 잡는가 (결함 버전 출력 ≠ 기대값) → 경계 사례 커버리지.
- 진단: 틀린 예시의 기대값이 어떤 결함 버전의 출력과 같으면, 그 결함이 어긴 정책을 오해한 것으로 본다.

저장: data/drills/sheets/<sheet_id>/sheet.json
"""
from datetime import datetime

from . import cards, policy, sessions

DEFAULT_SECONDS = 1200
MAX_ROWS = 60


class CriteriaError(RuntimeError):
    pass


def _left(sheet: dict) -> int | None:
    if not sheet["seconds"]:
        return None
    el = (datetime.now().astimezone() - datetime.fromisoformat(sheet["started_at"])).total_seconds()
    return int(sheet["seconds"] - el)


def _clean_rows(bundle: dict, rows: list[dict]) -> list[dict]:
    if len(rows) > MAX_ROWS:
        raise CriteriaError(f"예시는 최대 {MAX_ROWS}개입니다.")
    fns = {f["name"] for f in bundle["functions"]}
    out = []
    for r in rows:
        if r.get("func") not in fns:
            raise CriteriaError(f"없는 함수: {r.get('func')}")
        out.append({"func": r["func"], "args": {str(k): str(v) for k, v in (r.get("args") or {}).items()},
                    "expected": str(r.get("expected") or ""), "note": str(r.get("note") or "")[:200]})
    return out


def create(bundle_id: str, seconds: int | None = DEFAULT_SECONDS) -> dict:
    policy.load(bundle_id)
    sid = cards._new_id("c")
    sheet = {"sheet_id": sid, "bundle_id": bundle_id, "created_at": sessions.now_iso(),
             "started_at": sessions.now_iso(), "seconds": seconds or 0, "status": "in_progress",
             "rows": [], "result": None, "finished_at": None, "timed_out": False}
    cards._write("sheets", sid, sheet)
    return sheet


def load(sheet_id: str) -> dict:
    return cards._read("sheets", sheet_id)


def save_rows(sheet_id: str, rows: list[dict]) -> dict:
    sheet = load(sheet_id)
    if sheet["status"] != "in_progress":
        raise CriteriaError("이미 제출한 답안입니다.")
    sheet["rows"] = _clean_rows(policy.load(sheet["bundle_id"]), rows)
    cards._write("sheets", sheet_id, sheet)
    return {"saved": len(sheet["rows"])}


def submit(sheet_id: str, rows: list[dict], timed_out: bool = False) -> dict:
    sheet = load(sheet_id)
    if sheet["status"] != "in_progress":
        raise CriteriaError("이미 제출한 답안입니다.")
    bundle = policy.load(sheet["bundle_id"])
    sheet["rows"] = _clean_rows(bundle, rows)
    left = _left(sheet)
    sheet["timed_out"] = timed_out or (left is not None and left <= 0)
    sheet["finished_at"] = sessions.now_iso()
    sheet["elapsed_sec"] = int((datetime.now().astimezone() - datetime.fromisoformat(sheet["started_at"])).total_seconds())
    sheet["result"] = grade(bundle, sheet["rows"])
    sheet["status"] = "done"
    cards._write("sheets", sheet_id, sheet)
    return view(sheet)


# ---------- 채점 ----------

def grade(bundle: dict, rows: list[dict]) -> dict:
    fns = {f["name"]: f for f in bundle["functions"]}
    pols = {p["id"]: p for p in bundle["policies"]}
    defects = bundle["defects"]
    graded, cases, exp = [], [], {}
    for i, r in enumerate(rows, 1):
        fn = fns[r["func"]]
        g = {"n": i, "func": r["func"], "call": policy.example_label(fn, r["args"]), "expected": r["expected"],
             "note": r["note"], "verdict": "invalid", "actual": None, "error": None, "kills": [], "same_as": [], "diagnosis": []}
        try:
            cases.append(policy.make_case(str(i), fn, r["args"]))
            exp[str(i)] = policy.expected_of(fn, r["expected"])
        except ValueError as e:
            g["error"] = str(e)
        graded.append(g)
    runs = policy.run_variants(policy.variants(bundle), cases) if cases else {}
    for g in graded:
        k = str(g["n"])
        if k not in exp:
            continue
        ref = runs["ref"][k]
        if not ref["ok"]:
            g["verdict"], g["error"] = "error", ref["error"]
            continue
        g["actual"] = ref["value"]
        g["verdict"] = "correct" if ref["value"] == exp[k] else "wrong"
        for d in defects:
            v = runs[d["id"]][k]
            if g["verdict"] == "correct" and (not v["ok"] or v["value"] != exp[k]):
                g["kills"].append(d["id"])
            if g["verdict"] == "wrong" and v["ok"] and v["value"] == exp[k]:
                g["same_as"].append(d["id"])
        g["diagnosis"] = [_policy_ref(pols[d["policy_id"]], d) for d in defects if d["id"] in g["same_as"]]

    defect_rows = []
    for d in defects:
        p = pols[d["policy_id"]]
        by = [g["n"] for g in graded if d["id"] in g["kills"]]
        defect_rows.append({"id": d["id"], "summary": d["summary"], "killed_by": by, **_policy_ref(p, d),
                            "model_examples": [] if by else _model_examples(bundle, d)})
    valid = [g for g in graded if g["verdict"] in ("correct", "wrong")]
    correct = [g for g in valid if g["verdict"] == "correct"]
    killed = [d for d in defect_rows if d["killed_by"]]
    return {
        "rows": graded, "defects": defect_rows,
        "summary": {"rows": len(graded), "valid": len(valid), "correct": len(correct),
                    "invalid": len(graded) - len(valid),
                    "accuracy": round(100 * len(correct) / len(valid)) if valid else None,
                    "defects": len(defect_rows), "killed": len(killed),
                    "coverage": round(100 * len(killed) / len(defect_rows)) if defect_rows else None,
                    "idle": sum(1 for g in correct if not g["kills"]),
                    "misread": sorted({p["policy_id"] for g in graded for p in g.get("diagnosis", [])})},
        "policies": [{"id": p["id"], "title": p["title"], "statement": p["statement"], "location": p["location"],
                      "doc_quote": p["doc_quote"], "kind": p["kind"], "kind_label": policy.KIND_LABEL[p["kind"]],
                      "has_defect": any(d["policy_id"] == p["id"] for d in defects)} for p in bundle["policies"]],
    }


def _policy_ref(p: dict, d: dict) -> dict:
    return {"policy_id": p["id"], "policy_title": p["title"], "statement": p["statement"], "location": p["location"],
            "doc_quote": p["doc_quote"], "kind_label": policy.KIND_LABEL[p["kind"]], "defect_id": d["id"],
            "defect_summary": d["summary"]}


def _model_examples(bundle: dict, d: dict) -> list[dict]:
    """놓친 결함을 잡는 모범 예시: 출제 검증에서 이 결함을 잡은 그 정책의 예시."""
    fns = {f["name"]: f for f in bundle["functions"]}
    killers = set((bundle.get("verification") or {}).get("kills", {}).get(d["id"], []))
    p = next(x for x in bundle["policies"] if x["id"] == d["policy_id"])
    out = []
    for i, ex in enumerate(p["examples"], 1):
        if f"{p['id']}.{i}" in killers:
            args = {a["name"]: a["value"] for a in ex["args"]}
            out.append({"func": ex["func"], "call": policy.example_label(fns[ex["func"]], args),
                        "expected": ex["expected"], "note": ex["note"]})
    return out


# ---------- 화면 ----------

def view(sheet: dict) -> dict:
    bundle = policy.load(sheet["bundle_id"])
    left = _left(sheet) if sheet["status"] == "in_progress" else None
    return {"sheet_id": sheet["sheet_id"], "bundle_id": sheet["bundle_id"], "title": bundle["title"],
            "status": sheet["status"], "seconds": sheet["seconds"],
            "remaining_sec": None if left is None else max(0, left),
            "spec_doc": bundle["spec_doc"], "functions": policy.public_functions(bundle), "rows": sheet["rows"],
            "timed_out": sheet["timed_out"], "elapsed_sec": sheet.get("elapsed_sec"), "result": sheet["result"]}


def overview() -> dict:
    sheets = cards._all("sheets")
    bundles = []
    for b in reversed(policy.all_bundles()):
        mine = [s for s in sheets if s["bundle_id"] == b["bundle_id"] and s["status"] == "done"]
        best = max((s["result"]["summary"]["coverage"] or 0 for s in mine), default=None)
        bundles.append({**policy.summary(b), "sheets": len(mine), "best_coverage": best})
    titles = {b["bundle_id"]: b["title"] for b in bundles}
    rows = []
    for s in reversed(sheets):
        sm = (s["result"] or {}).get("summary") or {}
        rows.append({"sheet_id": s["sheet_id"], "bundle_id": s["bundle_id"], "title": titles.get(s["bundle_id"], "-"),
                     "status": s["status"], "created_at": s["created_at"], "rows": len(s["rows"]),
                     "accuracy": sm.get("accuracy"), "coverage": sm.get("coverage")})
    return {"bundles": bundles, "sheets": rows}
