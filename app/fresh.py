"""새 문제 드릴 (docs/drill_design.md §6): 결함을 심은 작은 저장소를 만들고 같은 카드 형식으로 출제한다.

G1 사양(코드, RNG) → G2 저장소 생성(LLM) → G3 코드 검사·파일 쓰기·테스트 실행(실패 사유를 피드백해 최대 3회)
→ git 초기 커밋 → G4~G6 카드 출제·실행 검증(cards.build_deck, 심은 결함과 미끼가 우선 출제 대상).

저장소: $MOCK_WORKSPACE_ROOT/drills/<repo_id>/ (DR-4, 앱 저장소 밖). 메타: data/drills/repos/<repo_id>/repo.json
결함 사양은 저장소에 넣지 않는다 (메타에만 둔다).
"""
import random
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from . import cards, llm, probe, prompts, sessions, submit
from .config import settings
from .schemas import FreshRepo

# (도메인, 시스템, 외부 모델 호출 필수 여부). 외부 호출이 필수가 아닌 업무 시스템이 절반 이상 (§6.2 G1).
SYSTEMS = [
    ("사내 총무", "출퇴근 셔틀 좌석 신청: 노선·출발 시각별 좌석 신청과 취소", False),
    ("공유오피스", "회의실 시간 단위 예약과 취소", False),
    ("식자재 매장", "재고 입고·출고 기록과 발주 기준 이하 품목 목록", False),
    ("구민 체육센터", "강좌 정원 신청과 취소 시 대기자 승격", False),
    ("소극장", "공연 좌석 선점과 결제 확정, 선점 만료", False),
    ("가전 AS 센터", "수리 접수와 처리 상태 변경 이력", False),
    ("동네 의원", "진료 예약 시간대 배정과 변경", False),
    ("물류센터", "출고 요청 승인과 재고 차감", False),
    ("사내 HR", "휴가 신청·승인과 잔여 일수 차감", False),
    ("캠핑장", "사이트별 숙박 예약(체크인·체크아웃 날짜)과 요금 계산", False),
    ("고객센터", "문의 접수 후 외부 모델로 유형 분류·요약해 담당 큐에 배정", True),
    ("학원", "상담 메모를 외부 모델로 요약해 수강생 기록에 저장", True),
]
EXTERNAL_OPTIONAL_RATE = 0.3  # 외부 호출이 필수가 아닌 시스템에서 보조 기능으로 외부 호출을 넣을 확률

# 서버 코드를 실행해 관찰할 수 있는 패턴만 (화면·타이밍 의존 패턴 제외).
FRESH_TAGS = ["EMPTY_AS_SUCCESS", "PARSE_FAIL_KEEPS_STATE", "FAIL_ALL_PENDING", "VALIDATE_WRONG_SOURCE",
              "DUP_BREAKS_LOAD", "LOST_UPDATE", "TZ_BOUNDARY", "CAPACITY_EDGE"]
BUSINESS_TAGS = ["LOST_UPDATE", "TZ_BOUNDARY", "CAPACITY_EDGE"]
EXTERNAL_ONLY = {"FAIL_ALL_PENDING"}

MAX_ATTEMPTS = 3
MAX_FILES = 10
MAX_CODE_LINES = 700
TEST_TIMEOUT_SEC = 180
ALLOWED_DEPS = {"fastapi", "uvicorn", "httpx", "pytest"}
REQUIRED = ["README.md", "CONTEXT.md", "DECISIONS.md", "requirements.txt", "main.py"]
FORBIDDEN = re.compile(r"버그|결함|의도적|미끼|TODO|FIXME|\bbug|\bdefect|\bbait|intentional", re.I)
GITIGNORE = ".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n"
FOCUS_TITLE = "이 저장소에 심어 둔 결함과 미끼 (결함마다 카드 1장 이상, 미끼 카드 1장. 응시자에게는 알리지 않음)"


class FreshError(RuntimeError):
    pass


# ---------- G1 사양 ----------

def tag_weights(days: int = 30) -> dict[str, int]:
    """최근 드릴에서 틀린 결함 태그 빈도 + 1 (라플라스 평활). 자주 틀리는 패턴이 더 자주 나온다."""
    since = datetime.now().astimezone() - timedelta(days=days)
    w = {t: 1 for t in FRESH_TAGS}
    for r in cards._all("runs"):
        try:
            if datetime.fromisoformat(r["created_at"]) < since:
                continue
        except (KeyError, ValueError):
            continue
        for t, n in ((r.get("summary") or {}).get("missed_tags") or {}).items():
            if t in w:
                w[t] += n
    return w


def _weighted_pick(rng: random.Random, pool: list[str], w: dict[str, int], k: int) -> list[str]:
    pool, out = list(pool), []
    while pool and len(out) < k:
        t = rng.choices(pool, weights=[w[x] for x in pool])[0]
        out.append(t)
        pool.remove(t)
    return out


def pick_spec(seed: int | None = None, recent_domains: set[str] = frozenset(),
              weights: dict[str, int] | None = None) -> dict:
    rng = random.Random(seed)
    fresh = [s for s in SYSTEMS if s[0] not in recent_domains] or SYSTEMS
    domain, system, needs_ext = rng.choice(fresh)
    external = needs_ext or rng.random() < EXTERNAL_OPTIONAL_RATE
    w = weights or tag_weights()
    business = _weighted_pick(rng, BUSINESS_TAGS, w, 1)  # 업무 시스템 패턴 최소 1개
    rest = [t for t in FRESH_TAGS if t not in business and (external or t not in EXTERNAL_ONLY)]
    defects = business + _weighted_pick(rng, rest, w, rng.randint(1, 2))
    return {"domain": domain, "system": system, "external": external, "defects": defects}


# ---------- G3 검사 ----------

def _code_lines(files: dict[str, str]) -> int:
    return sum(len(c.splitlines()) for p, c in files.items() if Path(p).suffix in {".py", ".js", ".html"})


def check(repo: FreshRepo, spec: dict) -> list[str]:
    """테스트 실행 전 코드로 확인할 수 있는 규칙."""
    errs = []
    files: dict[str, str] = {}
    for f in repo.files:
        p = f.path.strip()
        if not p or p.startswith(("/", ".git")) or ".." in Path(p).parts or "\\" in p:
            errs.append(f"잘못된 파일 경로: {f.path}")
        elif p in files:
            errs.append(f"파일 경로 중복: {p}")
        else:
            files[p] = f.content
    for name in REQUIRED:
        if name not in files:
            errs.append(f"필수 파일 없음: {name}")
    if not any(re.fullmatch(r"tests/test_\w+\.py", p) for p in files):
        errs.append("tests/test_*.py 없음")
    if len(files) > MAX_FILES:
        errs.append(f"파일 {len(files)}개 (최대 {MAX_FILES})")
    if (n := _code_lines(files)) > MAX_CODE_LINES:
        errs.append(f"코드 {n}줄 (최대 {MAX_CODE_LINES})")
    deps = {re.split(r"[<>=\[;\s]", line.strip(), maxsplit=1)[0].lower()
            for line in files.get("requirements.txt", "").splitlines() if line.strip() and not line.startswith("#")}
    if extra := deps - ALLOWED_DEPS:
        errs.append(f"허용되지 않은 의존성: {', '.join(sorted(extra))} (fastapi, uvicorn, httpx, pytest만)")
    for p, c in files.items():
        hits = {m.group(0) for m in FORBIDDEN.finditer(c)} | {t for t in cards.DEFECT_TAGS if t in c}
        if hits:
            errs.append(f"{p}: 결함을 알려 주는 단서({', '.join(sorted(hits))})")
    if "0.0.0.0" not in files.get("README.md", ""):
        errs.append("README에 0.0.0.0 바인딩 실행 명령이 없음")
    planted = {d.tag for d in repo.planted}
    if missing := set(spec["defects"]) - planted:
        errs.append(f"planted에 빠진 결함: {', '.join(sorted(missing))}")
    if not repo.bait.location.strip():
        errs.append("bait 위치가 비어 있음")
    return errs


def write_files(repo: FreshRepo, root: Path) -> None:
    root.mkdir(parents=True)
    for f in repo.files:
        target = (root / f.path.strip()).resolve()
        if not target.is_relative_to(root.resolve()):
            raise FreshError(f"저장소 밖 경로: {f.path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content.rstrip() + "\n", encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")


def run_tests(root: Path) -> tuple[bool, str]:
    """저장소 테스트를 probe와 같은 격리 환경(가짜 키, 닫힌 base URL)에서 실행한다."""
    cmd = ["uv", "run", "--no-project", "--with-requirements", "requirements.txt", "--with", "pytest",
           "--with", "httpx", "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]
    with tempfile.TemporaryDirectory(prefix="drill_fresh_") as tmp:
        try:
            r = subprocess.run(cmd, cwd=root, env=probe._env(Path(tmp) / "obs.json"), capture_output=True,
                               text=True, timeout=TEST_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            return False, f"테스트가 {TEST_TIMEOUT_SEC}초 안에 끝나지 않음"
    out = r.stdout + r.stderr
    return r.returncode == 0, "\n".join(out.splitlines()[-40:])


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


# ---------- 생성 ----------

def _repo_root(repo_id: str) -> Path:
    return settings.workspace_root / "drills" / repo_id


def _focus(repo: FreshRepo) -> str:
    lines = [f"- 결함 {d.tag} ({cards.DEFECT_TAGS.get(d.tag, '')}) @ {d.location}: {d.behavior}" for d in repo.planted]
    lines.append(f"- 미끼 @ {repo.bait.location}: {repo.bait.behavior} — 정답이 '안전하게 처리됨'인 카드 1장")
    return "\n".join(lines)


def generate_repo(seed: int | None = None) -> dict:
    """G1~G3. 테스트가 통과한 저장소를 만들고 메타를 저장한다. 카드는 아직 없다."""
    recent = {r["spec"]["domain"] for r in list(reversed(cards._all("repos")))[:3]}
    spec = pick_spec(seed, recent)
    repo_id = cards._new_id("f")
    root = _repo_root(repo_id)
    defects = "\n".join(f"  - {t}: {cards.DEFECT_TAGS[t]}" for t in spec["defects"])
    feedback, attempts = "", []
    for i in range(MAX_ATTEMPTS):
        system = prompts.load(
            "fresh_repo", domain=spec["domain"], system=spec["system"], defects=defects,
            external="있음 (보조 기능 한 곳)" if spec["external"] else "없음 (외부 모델을 부르지 않는다)",
            max_files=MAX_FILES, max_lines=MAX_CODE_LINES, retry_feedback=feedback)
        repo = llm.parse(model=settings.model_strong, system=system, user="저장소를 만들어라.", schema=FreshRepo,
                         purpose="fresh_repo")
        errs = check(repo, spec)
        if not errs:
            shutil.rmtree(root, ignore_errors=True)
            write_files(repo, root)
            ok, out = run_tests(root)
            if not ok:
                errs = ["저장소 테스트 실패 (결함은 테스트가 통과하는 상태로 숨겨야 함). 출력 끝부분:\n" + out]
        attempts.append({"attempt": i + 1, "errors": errs})
        if not errs:
            break
        shutil.rmtree(root, ignore_errors=True)
        feedback = "\n[이전 저장소가 검사에 실패한 이유 — 이번에는 반드시 고칠 것]\n" + "\n".join(f"- {e}" for e in errs) + "\n"
    else:
        raise FreshError("저장소 생성 실패 (3회): " + " / ".join(attempts[-1]["errors"])[:800])
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=mockdrill", "-c", "user.email=mockdrill@localhost", "commit", "-q", "-m", "initial")
    meta = {"repo_id": repo_id, "created_at": sessions.now_iso(), "title": repo.title, "root": str(root),
            "spec": spec, "planted": [d.model_dump() for d in repo.planted], "bait": repo.bait.model_dump(),
            "readme_command": submit.readme_command(root)[0], "attempts": attempts}
    cards._write("repos", repo_id, meta)
    return meta


def load(repo_id: str) -> dict:
    return cards._read("repos", repo_id)


def build_deck(repo_id: str, count: int = cards.DEFAULT_CARDS) -> dict:
    meta = load(repo_id)
    root = Path(meta["root"])
    if not root.is_dir():
        raise cards.CardError(f"저장소가 없습니다: {root}")
    repo = FreshRepo(title=meta["title"], files=[], planted=meta["planted"], bait=meta["bait"])
    source = {"kind": "fresh", "key": repo_id, "root": str(root), "label": f"[새 문제] {meta['title']}"}
    return cards.build_deck(root, source, count, _focus(repo), FOCUS_TITLE)


def create(count: int = cards.DEFAULT_CARDS, seed: int | None = None) -> dict:
    """저장소 생성 + 첫 덱. 카드 검증이 모자라도 저장소는 남기고 오류를 돌려준다 (카드 더 만들기로 재시도)."""
    meta = generate_repo(seed)
    try:
        deck = build_deck(meta["repo_id"], count)
    except cards.CardError as e:
        raise cards.CardError(f"저장소는 만들었지만({meta['root']}) 카드 생성에 실패했습니다: {e}") from e
    return {"repo_id": meta["repo_id"], "title": meta["title"], "root": meta["root"], "deck_id": deck["deck_id"],
            "generation": deck["generation"]}
