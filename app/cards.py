"""동작 추적 카드 드릴 (docs/drill_design.md).

카드 = 상황 하나 + 질문 칸 2~3개. 정답은 probe(상황을 주입해 코드를 실제로 실행하는 pytest)가 관찰한 값이다.

- 덱 생성: 출제(LLM) → 구조 검사 → probe 실행 → 예상과 다르거나 실패하면 검수(LLM: 관찰값 채택/probe 수정/폐기) → 재실행.
  정답은 항상 실행 관찰값으로 확정한다. 출제자 예상이 틀렸던 카드는 surprise로 표시한다.
- 드릴: 카드를 하나씩 풀고(코드 보고 / 코드 닫고) 즉시 채점. 채점은 정확 일치라 LLM을 부르지 않는다.

저장: data/drills/decks/<deck_id>/deck.json, data/drills/runs/<run_id>/run.json
"""
import json
import random
import re
import secrets
from datetime import datetime
from pathlib import Path

from . import codectx, llm, probe, prompts, sessions
from .config import settings
from .schemas import CardRepair, GeneratedCard, GeneratedDeck

DEFECT_TAGS = {
    "SYNC_BLOCK": "async 핸들러 안의 동기 외부 호출이 이벤트 루프를 막음",
    "READ_THEN_LIMIT": "본문 전체를 읽은 뒤에야 크기 검사",
    "EMPTY_AS_SUCCESS": "빈 응답·빈 객체가 오류가 아니라 정상 빈 결과로 처리됨",
    "PARSE_FAIL_KEEPS_STATE": "파싱 실패 시 이전 상태 유지, 성공 시에만 교체",
    "STALE_RESPONSE": "늦게 도착한 응답이 최신 입력의 결과를 덮어씀",
    "RESEND_ORIGINAL": "수정본이 아니라 원본을 재전송해 사용자 수정이 유실됨",
    "FAIL_ALL_PENDING": "타임아웃 하나가 실행 중·대기 중 요청을 모두 거절함",
    "VALIDATE_WRONG_SOURCE": "검증 기준값이 예상과 다른 데이터(이전 값·정규화 전 값)",
    "WEAK_TEST": "테스트가 요구의 일부만 단언해 위반이 있어도 통과",
    "CLIENT_STATE_LEAK": "화면을 비워도 메모리·전역 배열에 데이터가 남음",
    "DUP_BREAKS_LOAD": "기준 데이터 중복·충돌이 전체 로딩 실패로 번짐",
    "LOST_UPDATE": "동시 수정 시 나중 쓰기가 앞 쓰기를 덮어씀",
    "TZ_BOUNDARY": "날짜 경계·시간대 처리 오류",
    "CAPACITY_EDGE": "정원·좌석·재고의 경계값(=, 초과, 0) 처리",
}

DEFAULT_CARDS = 8
GEN_ATTEMPTS = 2
MIN_VERIFIED = 3
REPAIR_ROUNDS = 2
SECONDS = {"closed": 90, "open": 180}
OPEN_MAX_FILES = 3
OPEN_MAX_LINES = 400
SNIPPET_RADIUS = 5
SNIPPET_MAX_LINES = 160
COORD = re.compile(r"^\s*([^:\s]+):(\d+)(?:-(\d+))?")
ID_RE = re.compile(r"[\w]+")


class CardError(RuntimeError):
    pass


# ---------- 저장 ----------

def _base() -> Path:
    return settings.data_dir / "drills"


def _path(kind: str, oid: str) -> Path:
    if not re.fullmatch(r"[a-z]_\d{8}_\d{6}_[0-9a-f]{4}", oid):
        raise ValueError("잘못된 ID")
    return _base() / kind / oid / f"{kind[:-1]}.json"


def _read(kind: str, oid: str) -> dict:
    p = _path(kind, oid)
    if not p.exists():
        raise FileNotFoundError(oid)
    return json.loads(p.read_text(encoding="utf-8"))


def _write(kind: str, oid: str, obj: dict) -> None:
    p = _path(kind, oid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _all(kind: str) -> list[dict]:
    d = _base() / kind
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob(f"*/{kind[:-1]}.json"))] if d.exists() else []


def load_deck(deck_id: str) -> dict:
    return _read("decks", deck_id)


def load_run(run_id: str) -> dict:
    return _read("runs", run_id)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(2)}"


# ---------- 재료(출처) ----------

def _session_root(session_id: str) -> Path | None:
    d = sessions.session_dir(session_id)
    tar = d / "snapshot.tar.gz"
    if not tar.exists():
        return None
    return codectx.extract_snapshot(tar, d / "snapshot")


def _misconceptions(session_id: str) -> list[str]:
    p = sessions.session_dir(session_id) / "rehearsal.json"
    if not p.exists():
        return []
    r = json.loads(p.read_text(encoding="utf-8"))
    return [q["grade"]["misconception"] for q in r.get("questions", []) + r.get("followups", [])
            if q.get("grade") and q["grade"].get("misconception")]


def source_key(deck: dict) -> str:
    """덱 재료 키: 세션 덱은 세션 ID, 새 문제 덱은 저장소 ID."""
    s = deck["source"]
    return s.get("key") or s["session_id"]


def deck_root(deck: dict) -> Path | None:
    s = deck["source"]
    if s.get("kind") == "fresh":
        root = Path(s["root"])
        return root if root.is_dir() else None
    return _session_root(s["session_id"])


def sources() -> list[dict]:
    decks = _all("decks")
    out = []
    root_dir = settings.data_dir / "sessions"
    for d in sorted(root_dir.glob("*/snapshot.tar.gz")) if root_dir.exists() else []:
        sid = d.parent.name
        try:
            title = sessions.load_problem(sid).title
        except Exception:  # noqa: BLE001 — 표시용
            title = sid
        root = _session_root(sid)
        mine = [x for x in decks if source_key(x) == sid]
        out.append({"kind": "session", "key": sid, "session_id": sid, "label": title,
                    "runnable": bool(root and probe.supported(root)),
                    "cards": sum(len(x["cards"]) for x in mine), "decks": len(mine)})
    for r in reversed(_all("repos")):
        mine = [x for x in decks if source_key(x) == r["repo_id"]]
        out.append({"kind": "fresh", "key": r["repo_id"], "session_id": None, "label": f"[새 문제] {r['title']}",
                    "path": r["root"], "runnable": Path(r["root"]).is_dir(),
                    "cards": sum(len(x["cards"]) for x in mine), "decks": len(mine)})
    return out


# ---------- 채점 보조 ----------

def norm(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v).strip().strip("\"'").strip()
    return re.sub(r"\s+", "", s).lower()


def same(a, b) -> bool:
    na, nb = norm(a), norm(b)
    try:
        return float(na) == float(nb)
    except ValueError:
        return na == nb


def structure_errors(c: GeneratedCard) -> list[str]:
    errs = []
    ids = [s.id for s in c.slots]
    if not 2 <= len(c.slots) <= 3:
        errs.append(f"칸 수 {len(c.slots)}개 (2~3개)")
    if len(set(ids)) != len(ids) or not all(ID_RE.fullmatch(i) for i in ids):
        errs.append("칸 id가 중복되거나 형식이 잘못됨")
    for s in c.slots:
        if s.kind == "choice":
            if not 2 <= len(s.options) <= 4 or len(set(s.options)) != len(s.options):
                errs.append(f"{s.id}: 선택지 2~4개, 중복 없이")
            elif s.claimed not in s.options:
                errs.append(f"{s.id}: 예상 정답이 선택지에 없음")
        if not re.search(rf"observe\(\s*[\"']{re.escape(s.id)}[\"']", c.probe):
            errs.append(f"{s.id}: probe가 observe하지 않음")
    if re.search(r"def\s+observe\s*\(", c.probe):
        errs.append("probe가 observe를 다시 정의함")
    if "import" not in c.probe:
        errs.append("probe가 저장소 코드를 import하지 않음")
    return errs


INTERNAL_CODE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{1,2}\d{1,3}[a-z]?|#\d+(?:-\d+)?)(?![A-Za-z0-9])")
HARNESS_WORDS = ("가짜로", "호출 수를 센", "이벤트 루프", "밀리초", "monkeypatch", "몽키패치", "probe")
CONTEXT_MAX, SCENARIO_MAX = 220, 180


def text_errors(c: GeneratedCard, names: set[str] = frozenset()) -> list[str]:
    """응시자에게 보이는 문장 규칙: 내부 코드·식별자·하네스 용어 금지, 길이 제한."""
    errs = []
    texts = [("context", c.context), ("scenario", c.scenario)] + \
        [(f"{s.id} 질문", s.prompt) for s in c.slots] + [(f"{s.id} 선택지", o) for s in c.slots for o in s.options]
    for where, t in texts:
        codes = sorted(set(INTERNAL_CODE.findall(t)))
        if codes:
            errs.append(f"{where}: 프로젝트 내부 코드({', '.join(codes)}) — 뜻으로 풀어 쓸 것")
        words = [w for w in HARNESS_WORDS if w in t]
        if words:
            errs.append(f"{where}: 측정·내부 기술 용어({', '.join(words)}) — 사용자 쪽 사건으로 쓸 것")
        leaked = [n for n in names if re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", t)]
        if leaked:
            errs.append(f"{where}: 코드 식별자({', '.join(sorted(leaked)[:5])}) — 역할로 가리킬 것")
    if not c.context.strip():
        errs.append("context가 비어 있음")
    if len(c.context) > CONTEXT_MAX:
        errs.append(f"context {len(c.context)}자 (최대 {CONTEXT_MAX})")
    if len(c.scenario) > SCENARIO_MAX:
        errs.append(f"scenario {len(c.scenario)}자 (최대 {SCENARIO_MAX})")
    return errs


def observed_errors(c: GeneratedCard, res: dict) -> list[str]:
    """실행 결과가 정답으로 쓸 수 있는 상태인가 (통과, 모든 칸 관찰, choice 관찰값이 선택지 안)."""
    if not res["passed"]:
        return ["probe 실행 실패"]
    errs = []
    for s in c.slots:
        if s.id not in res["observed"]:
            errs.append(f"{s.id}: 관찰값 없음")
        elif s.kind == "choice" and str(res["observed"][s.id]) not in s.options:
            errs.append(f"{s.id}: 관찰값 {res['observed'][s.id]!r}이 선택지에 없음")
    return errs


def mismatches(c: GeneratedCard, res: dict) -> list[str]:
    return [s.id for s in c.slots if not same(res["observed"].get(s.id), s.claimed)]


# ---------- 덱 생성 ----------

def _card_text(c: GeneratedCard) -> str:
    slots = "\n".join(f"- {s.id} ({s.kind}) {s.prompt} 선택지={s.options} 예상={s.claimed}" for s in c.slots)
    return f"흐름: {c.context}\n상황: {c.scenario}\n{slots}\n설명: {c.explanation}\n근거: {', '.join(c.evidence)}"


def _repair(c: GeneratedCard, res: dict, ctx: codectx.CodeContext) -> CardRepair:
    system = prompts.load("card_repair", card=_card_text(c), probe=c.probe, passed="통과" if res["passed"] else "실패",
                          observed=json.dumps(res["observed"], ensure_ascii=False), output=res["output"],
                          code_excerpt=ctx.excerpt(c.evidence))
    return llm.parse(model=settings.model_strong, system=system, user="판정하라.", schema=CardRepair,
                     purpose="card_repair")


def verify(cards: list[GeneratedCard], root: Path, ctx: codectx.CodeContext) -> tuple[list[dict], list[dict]]:
    """카드를 실행으로 검증한다. 반환: (확정 카드, 폐기 기록). 확정 카드의 정답은 관찰값이다."""
    kept, dropped = [], []
    pending: dict[str, tuple[GeneratedCard, str | None]] = {}
    for c in cards:
        errs = structure_errors(c)
        if errs:
            dropped.append({"id": c.id, "reason": "; ".join(errs)})
        else:
            pending[c.id] = (c, None)
    for rnd in range(REPAIR_ROUNDS + 1):
        if not pending:
            break
        results = probe.run_many(root, {cid: c.probe for cid, (c, _) in pending.items()})
        nxt = {}
        for cid, (c, note) in pending.items():
            res = results[cid]
            bad = observed_errors(c, res)
            if not bad and not mismatches(c, res):
                kept.append(_finalize(c, res, surprise=False, note=note))
                continue
            if rnd == REPAIR_ROUNDS:
                dropped.append({"id": cid, "reason": "; ".join(bad) or "검수 후에도 예상과 다름"})
                continue
            rep = _repair(c, res, ctx)
            if rep.action == "accept_observed" and not bad and rep.explanation:
                kept.append(_finalize(c.model_copy(update={"explanation": rep.explanation}), res, surprise=True,
                                      note=rep.reason))
            elif rep.action == "fix_probe" and rep.probe:
                fixed = c.model_copy(update={"probe": rep.probe})
                errs = structure_errors(fixed)
                if errs:
                    dropped.append({"id": cid, "reason": "수정 probe 구조 오류: " + "; ".join(errs)})
                else:
                    nxt[cid] = (fixed, rep.reason)
            else:
                dropped.append({"id": cid, "reason": f"{rep.action}: {rep.reason}"})
        pending = nxt
    return kept, dropped


def _finalize(c: GeneratedCard, res: dict, surprise: bool, note: str | None) -> dict:
    slots = []
    for s in c.slots:
        ans = res["observed"][s.id]
        slots.append({"id": s.id, "prompt": s.prompt, "kind": s.kind, "options": s.options,
                      "answer": ans if s.kind == "choice" else norm(ans) if isinstance(ans, bool) else str(ans),
                      "claimed": s.claimed})
    return {"id": c.id, "context": c.context, "scenario": c.scenario, "slots": slots, "evidence": c.evidence, "explanation": c.explanation,
            "defect_tags": [t for t in c.defect_tags if t in DEFECT_TAGS], "probe": c.probe,
            "surprise": surprise, "repair_note": note}


def generate_deck(session_id: str, count: int = DEFAULT_CARDS) -> dict:
    root = _session_root(session_id)
    if root is None:
        raise CardError("제출 스냅샷이 없는 세션입니다.")
    mis = _misconceptions(session_id)
    source = {"kind": "session", "key": session_id, "session_id": session_id, "label": _label(session_id)}
    return build_deck(root, source, count, "\n".join(f"- {m}" for m in mis) or "(없음)")


def build_deck(root: Path, source: dict, count: int, focus: str,
               focus_title: str = "응시자가 이 코드에 대해 전에 했던 오해 (우선 출제 대상)") -> dict:
    """저장소 root로 카드를 출제·실행 검증해 덱을 저장한다. focus: 우선 출제 대상 (오해 목록 또는 심은 결함)."""
    if not probe.supported(root):
        raise CardError("Python 저장소만 실행 검증할 수 있습니다.")
    ctx = codectx.build(root)
    readme = next((ctx.read(n) for n in ("README.md", "readme.md") if ctx.read(n)), "(README 없음)")
    existing = [c["scenario"] for d in _all("decks") if source_key(d) == source["key"] for c in d["cards"]]
    if existing:
        focus += "\n\n[이미 출제된 카드 상황 — 겹치지 말 것]\n" + "\n".join(f"- {s}" for s in existing)
    system = prompts.load("card_generate", readme=readme[:6000], focus_title=focus_title, file_tree=ctx.tree_text(),
                          key_files=ctx.key_files_text(), misconceptions=focus, count=count, probe_dir=probe.PROBE_DIR,
                          tags="\n".join(f"- {k}: {v}" for k, v in DEFECT_TAGS.items()))
    names = _code_names(ctx)
    user, text_dropped, ok = "카드를 출제하라.", [], []
    for attempt in range(GEN_ATTEMPTS):
        out = llm.parse(model=settings.model_strong, system=system, user=user, schema=GeneratedDeck,
                        purpose="card_generate")
        bad = {c.id: text_errors(c, names) for c in out.cards}
        ok = [c for c in out.cards if not bad[c.id]]
        text_dropped = [{"id": cid, "reason": "; ".join(e)} for cid, e in bad.items() if e]
        if len(ok) >= count - 1 or attempt == GEN_ATTEMPTS - 1:
            break
        user = ("카드를 출제하라.\n[이전 출제가 문장 규칙을 어겨 다시 출제한다. 이번에는 반드시 고칠 것]\n"
                + "\n".join(f"- {d['id']}: {d['reason']}" for d in text_dropped))
    kept, dropped = verify(ok, root, ctx)
    dropped = text_dropped + dropped
    if len(kept) < MIN_VERIFIED:
        raise CardError(f"실행 검증을 통과한 카드가 {len(kept)}장뿐입니다 (최소 {MIN_VERIFIED}장). 다시 생성하세요. "
                        f"폐기 사유: " + " / ".join(f"{d['id']}: {d['reason']}" for d in dropped)[:800])
    deck_id = _new_id("k")
    deck = {"deck_id": deck_id, "created_at": sessions.now_iso(),
            "source": source,
            "cards": [{**c, "key": f"{deck_id}/{c['id']}"} for c in kept],
            "generation": {"requested": count, "verified": len(kept), "dropped": dropped}}
    _write("decks", deck_id, deck)
    return deck


def _code_names(ctx: codectx.CodeContext) -> set[str]:
    """문장에 나오면 안 되는 이름: 함수·클래스·코드 파일명 (문서 파일과 짧은 이름은 제외)."""
    docs = {n for rel in ctx.files for n in (Path(rel).name, Path(rel).stem)
            if Path(rel).suffix.lower() in {".md", ".txt", ".rst", ""}}
    return {n for n in ctx.identifiers() - docs if len(n) >= 4 and not n.startswith("/")}


def _label(session_id: str) -> str:
    try:
        return sessions.load_problem(session_id).title
    except Exception:  # noqa: BLE001
        return session_id


# ---------- 드릴 진행 ----------

def _card_stats() -> dict[str, dict]:
    st: dict[str, dict] = {}
    for r in _all("runs"):
        for it in r["items"]:
            if it["phase"] != "done":
                continue
            s = st.setdefault(it["card_key"], {"attempts": 0, "last_correct": None, "flagged": False})
            s["attempts"] += 1
            s["last_correct"] = it["all_correct"]
            s["flagged"] = s["flagged"] or it["flagged"]
    return st


def create_run(mode: str, count: int, source: str | None = None, seconds: int | None = None) -> dict:
    """source: 재료 키(세션 ID 또는 새 문제 저장소 ID). None이면 전체."""
    if mode not in SECONDS:
        raise CardError("모드는 closed 또는 open입니다.")
    pool = [(d, c) for d in _all("decks") if not source or source_key(d) == source
            for c in d["cards"]]
    st = _card_stats()
    pool = [(d, c) for d, c in pool if not st.get(c["key"], {}).get("flagged")]
    if not pool:
        raise CardError("풀 카드가 없습니다. 먼저 카드를 생성하세요.")
    random.shuffle(pool)

    def rank(dc):
        s = st.get(dc[1]["key"])
        if s is None:
            return 0          # 안 푼 카드
        return 1 if not s["last_correct"] else 2 + s["attempts"]  # 마지막에 틀린 카드, 그다음 덜 푼 카드
    pool.sort(key=rank)
    rid = _new_id("r")
    run = {"run_id": rid, "mode": mode, "seconds": SECONDS[mode] if seconds is None else seconds,
           "created_at": sessions.now_iso(), "status": "in_progress", "summary": None,
           "items": [{"n": i, "card_key": c["key"], "deck_id": d["deck_id"], "card_id": c["id"],
                      "source_label": d["source"]["label"], "phase": "pending", "started_at": None,
                      "answered_at": None, "elapsed_sec": None, "timed_out": False, "answers": None,
                      "results": None, "all_correct": None, "flagged": False}
                     for i, (d, c) in enumerate(pool[:count], 1)]}
    _write("runs", rid, run)
    return run


def _card(item: dict) -> dict:
    d = load_deck(item["deck_id"])
    return next(c for c in d["cards"] if c["id"] == item["card_id"])


def _item(run: dict, n: int) -> dict:
    it = next((x for x in run["items"] if x["n"] == n), None)
    if it is None:
        raise CardError("없는 카드입니다.")
    return it


def current(run: dict) -> dict | None:
    return next((x for x in run["items"] if x["phase"] != "done"), None)


def _left(run: dict, it: dict) -> int | None:
    if not run["seconds"] or not it["started_at"]:
        return None
    return int(run["seconds"] - (datetime.now().astimezone() - datetime.fromisoformat(it["started_at"])).total_seconds())


def _open_code(root: Path | None, card: dict) -> list[dict]:
    """코드 보고 모드: 근거 파일 전체를 강조 없이 보여준다 (어느 줄인지 알려주지 않는다)."""
    if root is None:
        return []
    ctx = codectx.build(root)
    files = []
    for e in card["evidence"]:
        m = COORD.match(e)
        if m and m.group(1) not in files:
            files.append(m.group(1))
    out = []
    for rel in files[:OPEN_MAX_FILES]:
        text = ctx.read(rel)
        if text is not None:
            lines = text.splitlines()[:OPEN_MAX_LINES]
            out.append({"file": rel, "lines": [{"n": i, "text": t} for i, t in enumerate(lines, 1)]})
    return out


def start_item(run_id: str, n: int) -> dict:
    run = load_run(run_id)
    it = _item(run, n)
    if current(run) is not it:
        raise CardError("순서대로 진행해야 합니다.")
    if it["phase"] == "pending":
        it["phase"], it["started_at"] = "answering", sessions.now_iso()
        _write("runs", run_id, run)
    return question_view(run, it)


def question_view(run: dict, it: dict) -> dict:
    c = _card(it)
    deck = load_deck(it["deck_id"])
    left = _left(run, it)
    out = {"n": it["n"], "total": len(run["items"]), "source_label": it["source_label"],
           "context": c.get("context", ""), "scenario": c["scenario"],
           "slots": [{k: s[k] for k in ("id", "prompt", "kind", "options")} for s in c["slots"]],
           "seconds": run["seconds"], "remaining_sec": None if left is None else max(0, left), "code": []}
    if run["mode"] == "open":
        out["code"] = _open_code(deck_root(deck), c)
    return out


def answer(run_id: str, n: int, answers: dict, timed_out: bool) -> dict:
    run = load_run(run_id)
    it = _item(run, n)
    if it["phase"] != "answering":
        raise CardError("답하는 중인 카드가 아닙니다.")
    c = _card(it)
    now = datetime.now().astimezone()
    left = _left(run, it)
    it["answers"] = {s["id"]: str(answers.get(s["id"], "") or "") for s in c["slots"]}
    it["results"] = {s["id"]: bool(it["answers"][s["id"]].strip()) and same(it["answers"][s["id"]], s["answer"])
                     for s in c["slots"]}
    it["all_correct"] = all(it["results"].values())
    it["answered_at"] = now.isoformat(timespec="seconds")
    it["elapsed_sec"] = int((now - datetime.fromisoformat(it["started_at"])).total_seconds())
    it["timed_out"] = timed_out or (left is not None and left <= 0)
    it["phase"] = "done"
    if current(run) is None:
        run["status"] = "done"
        run["finished_at"] = sessions.now_iso()
        run["summary"] = summary(run)
    _write("runs", run_id, run)
    return result_view(run, it)


def snippets(ctx: codectx.CodeContext | None, coords: list[str]) -> list[dict]:
    """근거 좌표 주변 줄을 파일별로 모아 겹치는 구간을 합친다. 근거 줄은 hit=True."""
    if ctx is None:
        return []
    hits: dict[str, set[int]] = {}
    for c in coords:
        m = COORD.match(c)
        if m:
            lo = int(m.group(2))
            hits.setdefault(m.group(1), set()).update(range(lo, min(int(m.group(3) or lo), lo + 40) + 1))
    out, used = [], 0
    for rel, lines_hit in hits.items():
        text = ctx.read(rel)
        if text is None:
            continue
        lines = text.splitlines()
        ranges: list[list[int]] = []
        for n in sorted(lines_hit):
            lo, hi = max(1, n - SNIPPET_RADIUS), min(len(lines), n + SNIPPET_RADIUS)
            if ranges and lo <= ranges[-1][1] + 1:
                ranges[-1][1] = max(ranges[-1][1], hi)
            else:
                ranges.append([lo, hi])
        for lo, hi in ranges:
            if lo > hi:
                continue
            if used + hi - lo + 1 > SNIPPET_MAX_LINES:
                return out
            out.append({"file": rel, "start": lo, "end": hi,
                        "lines": [{"n": i, "text": lines[i - 1], "hit": i in lines_hit} for i in range(lo, hi + 1)]})
            used += hi - lo + 1
    return out


def result_view(run: dict, it: dict) -> dict:
    if it["phase"] != "done":
        raise CardError("답한 뒤에 공개됩니다.")
    c = _card(it)
    deck = load_deck(it["deck_id"])
    root = deck_root(deck)
    return {
        "n": it["n"], "total": len(run["items"]), "source_label": it["source_label"],
        "context": c.get("context", ""), "scenario": c["scenario"],
        "slots": [{**{k: s[k] for k in ("id", "prompt", "kind", "options", "answer")},
                   "mine": it["answers"][s["id"]], "correct": it["results"][s["id"]]} for s in c["slots"]],
        "all_correct": it["all_correct"], "timed_out": it["timed_out"], "elapsed_sec": it["elapsed_sec"],
        "explanation": c["explanation"], "evidence": c["evidence"], "defect_tags": c["defect_tags"],
        "surprise": c["surprise"], "probe": c["probe"], "flagged": it["flagged"],
        "snippets": snippets(codectx.build(root) if root else None, c["evidence"]),
        "run_done": run["status"] == "done",
    }


def flag(run_id: str, n: int) -> dict:
    run = load_run(run_id)
    it = _item(run, n)
    if it["phase"] != "done":
        raise CardError("답한 뒤에 신고할 수 있습니다.")
    it["flagged"] = not it["flagged"]
    if run["status"] == "done":
        run["summary"] = summary(run)
    _write("runs", run_id, run)
    return {"flagged": it["flagged"]}


def summary(run: dict) -> dict:
    done = [it for it in run["items"] if it["phase"] == "done" and not it["flagged"]]
    slots = [v for it in done for v in it["results"].values()]
    tags: dict[str, int] = {}
    for it in done:
        if not it["all_correct"]:
            for t in _card(it)["defect_tags"]:
                tags[t] = tags.get(t, 0) + 1
    n = len(done)
    return {"cards": n, "cards_correct": sum(it["all_correct"] for it in done),
            "card_rate": round(100 * sum(it["all_correct"] for it in done) / n) if n else None,
            "slot_rate": round(100 * sum(slots) / len(slots)) if slots else None,
            "avg_sec": round(sum(it["elapsed_sec"] for it in done) / n) if n else None,
            "timed_out": sum(it["timed_out"] for it in done), "missed_tags": tags}


def run_view(run: dict) -> dict:
    cur = current(run)
    return {"run_id": run["run_id"], "mode": run["mode"], "seconds": run["seconds"], "status": run["status"],
            "created_at": run["created_at"], "total": len(run["items"]), "summary": run["summary"],
            "current": cur["n"] if cur else None,
            "items": [{"n": it["n"], "source_label": it["source_label"], "phase": it["phase"],
                       "all_correct": it["all_correct"], "flagged": it["flagged"]} for it in run["items"]]}


def list_runs() -> list[dict]:
    out = []
    for r in reversed(_all("runs")):
        s = r["summary"] or {}
        out.append({"run_id": r["run_id"], "mode": r["mode"], "status": r["status"], "created_at": r["created_at"],
                    "total": len(r["items"]), "done": sum(it["phase"] == "done" for it in r["items"]),
                    "card_rate": s.get("card_rate"), "slot_rate": s.get("slot_rate")})
    return out


def deck_list() -> list[dict]:
    return [{"deck_id": d["deck_id"], "label": d["source"]["label"], "key": source_key(d),
             "session_id": d["source"].get("session_id"),
             "created_at": d["created_at"], "cards": len(d["cards"]), **d["generation"]}
            for d in reversed(_all("decks"))]
