"""A. 읽기·정리 드릴 (docs/flow_drills_design.md 4절).

정책 묶음의 명세 문서를 읽고 요구사항 목록을 자기 말로 적는다. 채점은 LLM이 묶음의 정책 목록과 대조해
정책마다 반영 / 부분 반영 / 잘못 해석 / 누락을, 응시자 항목마다 정책 해당 / 문서에 있음(채점 외) / 근거 없음 / 문서와 어긋남을 판정한다.
결과는 "놓친 정책 + 문서 속 위치·원문 + 놓치면 생기는 일(결함 요약)".

저장: data/drills/reads/<read_id>/read.json
"""
import re
from datetime import datetime

from . import cards, llm, policy, prompts, sessions
from .config import settings
from .schemas import ReadingGradeOut

DEFAULT_SECONDS = 1500
MAX_CHARS = 20000
MAX_ITEMS = 150
BULLET = re.compile(r"^(?:[-*•·]|\d+[.)]|\[[ xX]?\])\s*")
VERDICT_LABEL = {"covered": "반영", "partial": "부분 반영", "misread": "잘못 해석", "missed": "누락"}
ITEM_LABEL = {"matched": "정책 해당", "extra": "문서에 있음 (채점 외)", "unsupported": "문서에 근거 없음",
              "contradicts": "문서와 어긋남"}


class ReadingError(RuntimeError):
    pass


def parse_items(text: str) -> list[dict]:
    """한 줄 = 한 항목. '#'로 시작하는 줄은 제목(다음 항목들의 묶음 이름). 글머리표·번호는 떼어 낸다."""
    items, heading = [], ""
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            heading = s.lstrip("#").strip()
            continue
        s = BULLET.sub("", s).strip()
        if s:
            items.append({"n": len(items) + 1, "text": s, "section": heading})
    return items


def _check_text(text: str) -> str:
    if len(text) > MAX_CHARS:
        raise ReadingError(f"최대 {MAX_CHARS:,}자입니다.")
    if len(parse_items(text)) > MAX_ITEMS:
        raise ReadingError(f"항목은 최대 {MAX_ITEMS}개입니다.")
    return text


def _left(sheet: dict) -> int | None:
    if not sheet["seconds"]:
        return None
    el = (datetime.now().astimezone() - datetime.fromisoformat(sheet["started_at"])).total_seconds()
    return int(sheet["seconds"] - el)


def create(bundle_id: str, seconds: int | None = DEFAULT_SECONDS) -> dict:
    policy.load(bundle_id)
    rid = cards._new_id("r")
    sheet = {"read_id": rid, "bundle_id": bundle_id, "created_at": sessions.now_iso(), "started_at": sessions.now_iso(),
             "seconds": seconds or 0, "status": "in_progress", "text": "", "result": None, "finished_at": None,
             "timed_out": False}
    cards._write("reads", rid, sheet)
    return sheet


def load(read_id: str) -> dict:
    return cards._read("reads", read_id)


def save_text(read_id: str, text: str) -> dict:
    sheet = load(read_id)
    if sheet["status"] != "in_progress":
        raise ReadingError("이미 제출한 답안입니다.")
    sheet["text"] = _check_text(text)
    cards._write("reads", read_id, sheet)
    return {"saved": len(parse_items(text))}


def submit(read_id: str, text: str, timed_out: bool = False) -> dict:
    """LLM 채점이 실패하면 답안만 저장된 채 진행 중으로 남아 다시 제출할 수 있다."""
    sheet = load(read_id)
    if sheet["status"] != "in_progress":
        raise ReadingError("이미 제출한 답안입니다.")
    sheet["text"] = _check_text(text)
    left = _left(sheet)
    sheet["timed_out"] = sheet["timed_out"] or timed_out or (left is not None and left <= 0)
    if not sheet.get("finished_at"):
        sheet["finished_at"] = sessions.now_iso()
        sheet["elapsed_sec"] = int((datetime.now().astimezone() - datetime.fromisoformat(sheet["started_at"])).total_seconds())
    cards._write("reads", read_id, sheet)
    bundle = policy.load(sheet["bundle_id"])
    items = parse_items(text)
    judged = judge(bundle, items, sheet["seconds"]) if items else None
    sheet["result"] = grade(bundle, items, judged)
    sheet["status"] = "done"
    cards._write("reads", read_id, sheet)
    return view(sheet)


# ---------- 채점 ----------

def _policies_text(bundle: dict) -> str:
    return "\n".join(f"- {p['id']} [{policy.KIND_LABEL[p['kind']]}] {p['title']}: {p['statement']}\n"
                     f"  위치: {p['location']} / 원문: \"{p['doc_quote']}\"" for p in bundle["policies"])


def _items_text(items: list[dict]) -> str:
    return "\n".join(f"{i['n']}. " + (f"[{i['section']}] " if i["section"] else "") + i["text"] for i in items)


def judge(bundle: dict, items: list[dict], seconds: int) -> ReadingGradeOut:
    time_note = f"{seconds // 60}분 안에" if seconds else "시간 제한 없이"
    system = prompts.load("reading_grade", time_note=time_note, spec_doc=bundle["spec_doc"],
                          policies=_policies_text(bundle), items=_items_text(items))
    return llm.parse(model=settings.model_strong, system=system, user="채점하라.", schema=ReadingGradeOut,
                     purpose="reading_grade")


def grade(bundle: dict, items: list[dict], judged: ReadingGradeOut | None) -> dict:
    """LLM 판정을 정리한다. 빠진 정책은 누락, 빠진 항목은 채점 외로 채우고, 없는 번호·정책은 버린다."""
    ns = {i["n"] for i in items}
    pj = {j.policy_id: j for j in judged.policies} if judged else {}
    ij = {j.n: j for j in judged.items} if judged else {}
    defects = {d["policy_id"]: d for d in bundle["defects"]}
    texts = {i["n"]: i["text"] for i in items}

    pols = []
    for p in bundle["policies"]:
        j = pj.get(p["id"])
        refs = sorted({n for n in (j.items if j else []) if n in ns})
        verdict = j.verdict if j else "missed"
        if verdict != "missed" and not refs:
            verdict = "missed"          # 근거 항목 없는 판정은 믿지 않는다
        d = defects.get(p["id"])
        pols.append({"id": p["id"], "title": p["title"], "statement": p["statement"], "location": p["location"],
                     "doc_quote": p["doc_quote"], "kind": p["kind"], "kind_label": policy.KIND_LABEL[p["kind"]],
                     "verdict": verdict, "verdict_label": VERDICT_LABEL[verdict],
                     "items": [{"n": n, "text": texts[n]} for n in (refs if verdict != "missed" else [])],
                     "comment": (j.comment if j and verdict != "covered" else ""),
                     "if_missed": d["summary"] if d else None})

    rows = []
    for i in items:
        j = ij.get(i["n"])
        v = j.verdict if j else "extra"
        rows.append({**i, "verdict": v, "verdict_label": ITEM_LABEL[v], "comment": j.comment if j else ""})

    n = len(pols)
    cnt = {k: sum(1 for p in pols if p["verdict"] == k) for k in VERDICT_LABEL}
    by_kind = []
    for k, label in policy.KIND_LABEL.items():
        ps = [p for p in pols if p["kind"] == k]
        if ps:
            by_kind.append({"kind": k, "label": label, "total": len(ps),
                            "covered": sum(1 for p in ps if p["verdict"] == "covered")})
    return {
        "policies": pols, "items": rows, "overall": judged.overall if judged else "",
        "summary": {"policies": n, **cnt,
                    "recall": round(100 * (cnt["covered"] + 0.5 * cnt["partial"]) / n) if n else None,
                    "items": len(items),
                    "unsupported": sum(1 for r in rows if r["verdict"] == "unsupported"),
                    "contradicts": sum(1 for r in rows if r["verdict"] == "contradicts"),
                    "extra": sum(1 for r in rows if r["verdict"] == "extra"),
                    "by_kind": by_kind},
    }


# ---------- 화면 ----------

def view(sheet: dict) -> dict:
    bundle = policy.load(sheet["bundle_id"])
    left = _left(sheet) if sheet["status"] == "in_progress" else None
    return {"read_id": sheet["read_id"], "bundle_id": sheet["bundle_id"], "title": bundle["title"],
            "status": sheet["status"], "seconds": sheet["seconds"],
            "remaining_sec": None if left is None else max(0, left),
            "spec_doc": bundle["spec_doc"], "text": sheet["text"], "timed_out": sheet["timed_out"],
            "elapsed_sec": sheet.get("elapsed_sec"), "result": sheet["result"]}


def overview() -> dict:
    reads = cards._all("reads")
    done = policy.drills_done()
    bundles = []
    for b in reversed(policy.all_bundles()):
        mine = [r for r in reads if r["bundle_id"] == b["bundle_id"] and r["status"] == "done"]
        best = max((r["result"]["summary"]["recall"] or 0 for r in mine), default=None)
        # 다른 드릴(B·C·D)을 했으면 정책 목록을 이미 봤다
        bundles.append({**policy.summary(b), "reads": len(mine), "best_recall": best,
                        "seen": bool(mine) or bool(done.get(b["bundle_id"], set()) - {"A"})})
    titles = {b["bundle_id"]: b["title"] for b in bundles}
    rows = []
    for r in reversed(reads):
        sm = (r["result"] or {}).get("summary") or {}
        rows.append({"read_id": r["read_id"], "bundle_id": r["bundle_id"], "title": titles.get(r["bundle_id"], "-"),
                     "status": r["status"], "created_at": r["created_at"], "items": len(parse_items(r["text"])),
                     "recall": sm.get("recall"), "misread": sm.get("misread"),
                     "invented": (sm.get("unsupported") or 0) + (sm.get("contradicts") or 0) if sm else None})
    return {"bundles": bundles, "reads": rows}
