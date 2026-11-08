"""정책 묶음 생성 엔진 (docs/flow_drills_design.md 3절). B·A·C·D 드릴의 공통 재료.

P1 사양(도메인·설계 개념, RNG) → P2 출제(LLM: 명세 문서·함수·정책·정답 구현·결함 패치)
→ P3 검사(정적 검사, 정책 예시를 정답 구현에 실행해 기대값 확인, 결함 패치 적용 후 자기 정책 예시로 잡히는지 확인)
→ 실패 사유를 피드백해 최대 3회.

정답은 LLM 주장이 아니라 실행 관찰값으로 확인한다 (동작 추적 카드와 같은 원칙). 정답 구현·결함 버전은
앱 프로세스 밖에서 policy_harness로 실행한다.

저장: data/drills/bundles/<bundle_id>/bundle.json
"""
import ast
import difflib
import json
import random
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cards, fresh, generator, llm, policy_harness, prompts, sessions
from .config import settings
from .schemas import PolicyBundleOut

MAX_ATTEMPTS = 3
DOC_LEN = {"target": (6000, 9000), "accept": (4500, 14000)}
POLICIES = (8, 12)
FUNCTIONS = (3, 5)
DEFECTS = (5, 7)
COUNTERINTUITIVE_MIN = 2
RUN_TIMEOUT_SEC = 30
ALLOWED_IMPORTS = {"datetime", "math", "decimal", "fractions", "re", "dataclasses", "typing", "functools",
                   "collections", "enum", "itertools", "calendar", "bisect", "heapq", "statistics", "string",
                   "__future__"}
BANNED_CALLS = {"open", "exec", "eval", "compile", "__import__", "input", "breakpoint", "exit", "quit", "globals",
                "locals", "vars", "setattr", "delattr"}
DOC_BANNED = generator.BANNED_README_TERMS + ["결함", "버그", "정답 구현", "테스트 케이스"]
IDENT = re.compile(r"[a-z_][a-z0-9_]*")

# 예시로 실행 확인할 수 있는 설계 개념 (generator.CONCEPTS의 id). main: 하나 이상, side: 하나.
MAIN_CONCEPTS = ["business_date", "capacity_waitlist", "template_instance", "effective_period", "state_transition"]
SIDE_CONCEPTS = ["interval_overlap", "deterministic_tiebreak", "permissions"]
KIND_LABEL = {"general": "일반", "counterintuitive": "직관에 어긋남", "constraint": "제약", "conflict": "충돌·우선순위"}


class PolicyError(RuntimeError):
    pass


# ---------- P1 사양 ----------

def pick_spec(seed: int | None = None, recent_domains: set[str] = frozenset(),
              recent_concepts: set[str] = frozenset()) -> dict:
    rng = random.Random(seed)
    systems = [s for s in fresh.SYSTEMS if not s[2]]
    domain, system, _ = rng.choice([s for s in systems if s[0] not in recent_domains] or systems)
    mains = [c for c in MAIN_CONCEPTS if c not in recent_concepts] or MAIN_CONCEPTS
    first = rng.choice(mains)
    # 무거운 개념은 하나만 (generator 규칙과 같음)
    rest = [c for c in mains if c != first and not (generator.CONCEPTS[c]["heavy"] and generator.CONCEPTS[first]["heavy"])]
    chosen = [first] + ([rng.choice(rest)] if rest and rng.random() < 0.5 else []) + [rng.choice(SIDE_CONCEPTS)]
    return {"domain": domain, "system": system, "concepts": chosen, "year": 2026}


def concepts_text(ids: list[str]) -> str:
    out = []
    for cid in ids:
        c = generator.CONCEPTS[cid]
        qs = "; ".join(c["policy_questions"])
        out.append(f"- {c['name']}: 정할 업무 판단 — {qs}\n  순진한 구현의 실패: {c['naive_failure']}")
    return "\n".join(out)


# ---------- 실행 ----------

def static_errors(code: str, where: str = "정답 구현") -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"{where}: 문법 오류 {e.msg} ({e.lineno}행)"]
    errs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods = [(node.module or "").split(".")[0]]
        else:
            mods = []
        if bad := [m for m in mods if m not in ALLOWED_IMPORTS]:
            errs.append(f"{where}: 허용되지 않은 import {', '.join(bad)}")
        if isinstance(node, ast.Name) and node.id in BANNED_CALLS:
            errs.append(f"{where}: 사용 금지 이름 {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr not in {"__name__", "__init__"}:
            errs.append(f"{where}: 사용 금지 속성 {node.attr}")
        if isinstance(node, ast.Attribute) and node.attr in {"now", "today", "utcnow"}:
            errs.append(f"{where}: 현재 시각을 쓰면 안 됨 (.{node.attr})")
    return sorted(set(errs))


def defined_functions(code: str) -> dict[str, list[str]]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return {}
    return {n.name: [a.arg for a in n.args.args + n.args.kwonlyargs]
            for n in tree.body if isinstance(n, ast.FunctionDef)}


def apply_edits(code: str, edits) -> str:
    for i, e in enumerate(edits, 1):
        find = e["find"] if isinstance(e, dict) else e.find
        repl = e["replace"] if isinstance(e, dict) else e.replace
        n = code.count(find) if find else 0
        if n != 1:
            raise PolicyError(f"치환 {i}: find 문자열이 정답 구현에 {n}번 나옴 (정확히 1번이어야 함)")
        code = code.replace(find, repl)
    return code


def run_cases(code: str, cases: list[dict]) -> dict[str, dict]:
    """cases: [{id, func, params: [{name, type, options, raw}]}] → {id: {ok, value|error}}."""
    if not cases:
        return {}
    with tempfile.TemporaryDirectory(prefix="policy_run_") as tmp:
        d = Path(tmp)
        (d / "impl.py").write_text(code, encoding="utf-8")
        shutil.copy(Path(policy_harness.__file__), d / "harness.py")
        try:
            r = subprocess.run([sys.executable, "-I", "harness.py", "impl.py"], cwd=d, input=json.dumps({"cases": cases}),
                               capture_output=True, text=True, timeout=RUN_TIMEOUT_SEC,
                               env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"})
            rows = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError):
            rows = None
        if rows is None:
            err = "실행 실패 (시간 초과 또는 비정상 종료)"
            return {c["id"]: {"ok": False, "error": err} for c in cases}
    return {x["id"]: x for x in rows}


def run_variants(codes: dict[str, str], cases: list[dict]) -> dict[str, dict[str, dict]]:
    """여러 구현(정답 'ref'와 결함 버전들)에 같은 케이스를 병렬로 돌린다."""
    keys = list(codes)
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(zip(keys, pool.map(lambda k: run_cases(codes[k], cases), keys)))


def make_case(cid: str, fn: dict, args: dict[str, str]) -> dict:
    """함수 사양과 인자 문자열로 하네스 케이스를 만든다. 형식 오류는 ValueError."""
    params = []
    for p in fn["params"]:
        raw = args.get(p["name"], "")
        policy_harness.parse(p["type"], raw, p["options"])  # 형식 검사
        params.append({"name": p["name"], "type": p["type"], "options": p["options"], "raw": raw})
    return {"id": cid, "func": fn["name"], "params": params}


def expected_of(fn: dict, raw: str) -> str:
    r = fn["returns"]
    return policy_harness.expected_canon(r["type"], raw, r["options"])


def variants(bundle: dict) -> dict[str, str]:
    out = {"ref": bundle["reference_code"]}
    for d in bundle["defects"]:
        out[d["id"]] = apply_edits(bundle["reference_code"], d["edits"])
    return out


def example_label(fn: dict, args: dict[str, str]) -> str:
    """화면용: 한국어 라벨로 쓴 호출 모양."""
    def val(p: dict) -> str:
        v = str(args.get(p["name"], "")).strip()
        return v.replace("T", " ") if p["type"] == "datetime" else v or "(없음)"
    return ", ".join(f"{p['label']} {val(p)}" for p in fn["params"])


# ---------- P3 검사 ----------

def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t)


def check(out: PolicyBundleOut) -> tuple[list[str], dict]:
    """출제 결과 검사. 반환: (오류 목록, 검증 기록). 오류가 없을 때만 검증 기록이 완전하다."""
    errs: list[str] = []
    b = out.model_dump()
    doc = b["spec_doc"]
    n = len(doc.strip())
    if not DOC_LEN["accept"][0] <= n <= DOC_LEN["accept"][1]:
        errs.append(f"명세 문서 {n}자 (허용 {DOC_LEN['accept'][0]}~{DOC_LEN['accept'][1]}자)")
    if bad := [t for t in DOC_BANNED if t.lower() in doc.lower()]:
        errs.append(f"명세 문서에 쓰면 안 되는 말: {', '.join(bad)}")

    fns = {f["name"]: f for f in b["functions"]}
    if not FUNCTIONS[0] <= len(fns) <= FUNCTIONS[1] or len(fns) != len(b["functions"]):
        errs.append(f"함수 {len(b['functions'])}개 (이름 중복 없이 {FUNCTIONS[0]}~{FUNCTIONS[1]}개)")
    for f in b["functions"]:
        names = [p["name"] for p in f["params"]]
        if not IDENT.fullmatch(f["name"]) or not all(IDENT.fullmatch(x) for x in names) or len(set(names)) != len(names):
            errs.append(f"{f['name']}: 함수·인자 이름은 snake_case, 인자 이름 중복 없이")
        if not f["params"]:
            errs.append(f"{f['name']}: 인자가 없음")
        for p in f["params"] + [f["returns"]]:
            if p["type"] == "choice" and len(set(p["options"])) < 2:
                errs.append(f"{f['name']}: choice는 선택지 2개 이상")
        if f["name"] in doc:
            errs.append(f"명세 문서에 함수 이름 {f['name']}이 나옴")

    errs += static_errors(b["reference_code"])
    defined = defined_functions(b["reference_code"])
    for name, f in fns.items():
        if name not in defined:
            errs.append(f"정답 구현에 {name} 함수가 없음")
        elif set(defined[name]) != {p["name"] for p in f["params"]}:
            errs.append(f"{name}: 정답 구현 인자 {defined[name]}가 functions 사양과 다름")

    pols = b["policies"]
    ids = [p["id"] for p in pols]
    if not POLICIES[0] <= len(pols) <= POLICIES[1] or len(set(ids)) != len(ids):
        errs.append(f"정책 {len(pols)}개 (id 중복 없이 {POLICIES[0]}~{POLICIES[1]}개)")
    kinds = [p["kind"] for p in pols]
    if kinds.count("counterintuitive") < COUNTERINTUITIVE_MIN:
        errs.append(f"직관에 어긋나는 정책 {kinds.count('counterintuitive')}개 (최소 {COUNTERINTUITIVE_MIN}개)")
    for k in ("constraint", "conflict"):
        if k not in kinds:
            errs.append(f"{k} 정책이 없음 (최소 1개)")
    ndoc = _norm(doc)
    cases, owner, exp = [], {}, {}
    for p in pols:
        q = _norm(p["doc_quote"])
        if len(q) < 15 or q not in ndoc:
            errs.append(f"{p['id']}: doc_quote가 명세 문서 원문 그대로가 아니거나 너무 짧음")
        if not p["examples"]:
            errs.append(f"{p['id']}: 예시가 없음")
        for i, ex in enumerate(p["examples"], 1):
            eid = f"{p['id']}.{i}"
            fn = fns.get(ex["func"])
            if fn is None:
                errs.append(f"{eid}: 없는 함수 {ex['func']}")
                continue
            args = {a["name"]: a["value"] for a in ex["args"]}
            if set(args) != {x["name"] for x in fn["params"]}:
                errs.append(f"{eid}: 인자 {sorted(args)}가 {fn['name']}의 인자와 다름")
                continue
            try:
                cases.append(make_case(eid, fn, args))
                exp[eid] = expected_of(fn, ex["expected"])
                owner[eid] = p["id"]
            except ValueError as e:
                errs.append(f"{eid}: {e}")
    if errs:
        return errs, {}

    ref = run_cases(b["reference_code"], cases)
    for c in cases:
        r = ref[c["id"]]
        if not r["ok"]:
            errs.append(f"{c['id']}: 정답 구현 실행 오류 {r['error']}")
        elif r["value"] != exp[c["id"]]:
            errs.append(f"{c['id']}: 정답 구현 결과 {r['value']!r} ≠ 예시 기대값 {exp[c['id']]!r} "
                        "(문서·정답 구현·예시 중 틀린 쪽을 고칠 것)")

    dids = [d["id"] for d in b["defects"]]
    if not DEFECTS[0] <= len(dids) <= DEFECTS[1] or len(set(dids)) != len(dids):
        errs.append(f"결함 {len(dids)}개 (id 중복 없이 {DEFECTS[0]}~{DEFECTS[1]}개)")
    targets = [d["policy_id"] for d in b["defects"]]
    if len(set(targets)) != len(targets):
        errs.append("결함마다 다른 정책을 어겨야 함")
    codes = {}
    for d in b["defects"]:
        if d["policy_id"] not in ids:
            errs.append(f"{d['id']}: 없는 정책 {d['policy_id']}")
            continue
        try:
            code = apply_edits(b["reference_code"], d["edits"])
        except PolicyError as e:
            errs.append(f"{d['id']}: {e}")
            continue
        if code == b["reference_code"]:
            errs.append(f"{d['id']}: 치환 후에도 정답 구현과 같음")
            continue
        se = static_errors(code, d["id"])
        if se:
            errs += se
            continue
        codes[d["id"]] = code
    if errs:
        return errs, {}

    runs = run_variants(codes, cases)
    kills: dict[str, list[str]] = {}
    for d in b["defects"]:
        res = runs[d["id"]]
        killed = [c["id"] for c in cases if not res[c["id"]]["ok"] or res[c["id"]]["value"] != exp[c["id"]]]
        own = [k for k in killed if owner[k] == d["policy_id"]]
        if not own:
            errs.append(f"{d['id']}: {d['policy_id']}의 예시가 결함 버전에서도 같은 값을 냄 (잡을 수 없는 결함). "
                        "결함을 그 정책의 경계 예시가 드러내게 고치거나 예시를 추가할 것")
        kills[d["id"]] = killed
    return errs, {"examples": len(cases), "kills": kills}


# ---------- 생성 ----------

def _recent(n: int) -> list[dict]:
    return list(reversed(cards._all("bundles")))[:n]


def generate(seed: int | None = None) -> dict:
    recent = _recent(3)
    spec = pick_spec(seed, {b["spec"]["domain"] for b in recent},
                     {b["spec"]["concepts"][0] for b in recent[:2]})
    feedback, attempts = "", []
    for i in range(MAX_ATTEMPTS):
        system = prompts.load(
            "policy_bundle", domain=spec["domain"], system=spec["system"], concepts=concepts_text(spec["concepts"]),
            year=spec["year"], doc_min=DOC_LEN["target"][0], doc_max=DOC_LEN["target"][1],
            policy_min=POLICIES[0], policy_max=POLICIES[1], counterintuitive_min=COUNTERINTUITIVE_MIN,
            defect_min=DEFECTS[0], defect_max=DEFECTS[1], allowed_imports=", ".join(sorted(ALLOWED_IMPORTS - {"__future__"})),
            retry_feedback=feedback)
        out = llm.parse(model=settings.model_strong, system=system, user="정책 묶음을 만들어라.",
                        schema=PolicyBundleOut, purpose="policy_bundle")
        errs, verification = check(out)
        attempts.append({"attempt": i + 1, "errors": errs})
        if not errs:
            break
        feedback = ("\n[이전 출력이 검사에 실패한 이유 — 이번에는 반드시 고칠 것]\n"
                    + "\n".join(f"- {e}" for e in errs[:25]) + "\n")
    else:
        raise PolicyError("정책 묶음 생성 실패 (3회): " + " / ".join(attempts[-1]["errors"])[:800])
    bid = cards._new_id("p")
    bundle = {"bundle_id": bid, "created_at": sessions.now_iso(), "spec": spec, **out.model_dump(),
              "verification": verification, "attempts": attempts}
    cards._write("bundles", bid, bundle)
    return bundle


def load(bundle_id: str) -> dict:
    return cards._read("bundles", bundle_id)


def all_bundles() -> list[dict]:
    return cards._all("bundles")


def public_functions(bundle: dict) -> list[dict]:
    """응시자에게 보이는 함수 모양 (정책·구현은 빼고)."""
    return [{"name": f["name"], "summary": f["summary"],
             "params": [{k: p[k] for k in ("name", "label", "type", "options")} for p in f["params"]],
             "returns": {k: f["returns"][k] for k in ("label", "type", "options")}} for f in bundle["functions"]]


def summary(bundle: dict) -> dict:
    return {"bundle_id": bundle["bundle_id"], "title": bundle["title"], "created_at": bundle["created_at"],
            "domain": bundle["spec"]["domain"],
            "concepts": [generator.CONCEPTS[c]["name"] for c in bundle["spec"]["concepts"]],
            "doc_chars": len(bundle["spec_doc"]), "functions": len(bundle["functions"]),
            "policies": len(bundle["policies"]), "defects": len(bundle["defects"])}


# ---------- 예시·코드 조각 (C·D 드릴 공통) ----------

def policy_examples(bundle: dict) -> list[dict]:
    """정책 예시 전부: [{eid, policy_id, func, args, expected(정규형), note, call, case}]. 묶음은 이미 검증됨."""
    fns = {f["name"]: f for f in bundle["functions"]}
    out = []
    for p in bundle["policies"]:
        for i, ex in enumerate(p["examples"], 1):
            eid = f"{p['id']}.{i}"
            fn = fns[ex["func"]]
            args = {a["name"]: a["value"] for a in ex["args"]}
            out.append({"eid": eid, "policy_id": p["id"], "func": ex["func"], "args": args,
                        "expected": expected_of(fn, ex["expected"]), "note": ex["note"],
                        "call": example_label(fn, args), "case": make_case(eid, fn, args)})
    return out


def chunks(code: str) -> list[dict]:
    """모듈 최상위 정의(함수·상수·클래스)와 줄 범위(1부터). import·문서 문자열은 뺀다.
    refs: 이 정의가 참조하는 다른 이름 (호출·상수 의존 추적용)."""
    out = []
    for node in ast.parse(code).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) or (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)):
            continue
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names, kind = [node.name], "class" if isinstance(node, ast.ClassDef) else "func"
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
            kind = "const"
        else:
            names, kind = [], "other"
        refs = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} - set(names)
        out.append({"name": names[0] if names else f"{start}행", "names": names, "kind": kind,
                    "start": start, "end": node.end_lineno, "refs": sorted(refs)})
    return out


def changed_lines(ref: str, var: str) -> list[int]:
    """var 쪽에서 ref와 달라진 줄 번호. 줄이 지워지기만 한 곳은 지워진 자리 앞뒤 줄을 표시한다."""
    a, b = ref.splitlines(), var.splitlines()
    out: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
        elif tag == "delete":
            out.update(x for x in (j1, j1 + 1) if 1 <= x <= len(b))
    return sorted(out)


def touched_chunks(code: str, lines: list[int]) -> list[str]:
    """줄 번호가 속한 최상위 정의 이름 (모듈 순서). 정의 밖 줄은 바로 앞 정의로 본다."""
    chs = chunks(code)
    hit = []
    for ln in lines:
        inside = [c for c in chs if c["start"] <= ln <= c["end"]]
        before = [c for c in chs if c["end"] < ln]
        c = inside[0] if inside else (before[-1] if before else (chs[0] if chs else None))
        if c and c["name"] not in hit:
            hit.append(c["name"])
    order = [c["name"] for c in chs]
    return sorted(hit, key=order.index)


def reach(code: str) -> dict[str, set[str]]:
    """정의마다 직간접으로 의존하는 최상위 정의 이름 (자기 포함)."""
    chs = {c["name"]: c for c in chunks(code)}
    owner = {n: c["name"] for c in chs.values() for n in c["names"]}
    deps = {name: {owner[r] for r in c["refs"] if r in owner} for name, c in chs.items()}
    out = {}
    for name in chs:
        seen, stack = {name}, [name]
        while stack:
            for d in deps[stack.pop()]:
                if d not in seen:
                    seen.add(d)
                    stack.append(d)
        out[name] = seen
    return out


def related_policies(bundle: dict, names: set[str], code: str | None = None) -> list[str]:
    """이 정의들에 결과가 달린 정책: 예시의 함수가 이 정의를 직간접으로 쓰는 정책 (id 순서)."""
    r = reach(code or bundle["reference_code"])
    out = []
    for p in bundle["policies"]:
        if any(names & r.get(ex["func"], {ex["func"]}) for ex in p["examples"]):
            out.append(p["id"])
    return out


def snippet(code: str, names: list[str], hits: list[int] = ()) -> list[dict]:
    """화면용 코드 조각: 정의마다 {name, start, end, lines:[{n, text, hit}]}."""
    lines = code.splitlines()
    hs = set(hits)
    out = []
    for c in chunks(code):
        if c["name"] in names:
            out.append({"name": c["name"], "start": c["start"], "end": c["end"],
                        "lines": [{"n": n, "text": lines[n - 1], "hit": n in hs}
                                  for n in range(c["start"], c["end"] + 1)]})
    return out


def policy_ref(p: dict) -> dict:
    return {"id": p["id"], "title": p["title"], "statement": p["statement"], "location": p["location"],
            "doc_quote": p["doc_quote"], "kind": p["kind"], "kind_label": KIND_LABEL[p["kind"]]}


def drills_done() -> dict[str, set[str]]:
    """묶음마다 끝낸 드릴 (A 읽기, B 성공 기준, C 변경 검토, D 변경 대응). 결과 화면에서 정답 재료를 본 것."""
    out: dict[str, set[str]] = {}
    for kind, tag, done in (("reads", "A", lambda x: x["status"] == "done"),
                            ("sheets", "B", lambda x: x["status"] == "done"),
                            ("reviews", "C", lambda x: any(c.get("answer") for c in x["cards"])),
                            ("responses", "D", lambda x: any(c.get("answer") for c in x["cards"]))):
        for x in cards._all(kind):
            if done(x):
                out.setdefault(x["bundle_id"], set()).add(tag)
    return out
