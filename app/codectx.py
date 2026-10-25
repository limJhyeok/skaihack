"""코드 맥락 만들기: 파일 트리 + 줄 번호가 붙은 핵심 파일 (토큰 상한). 리허설 출제·채점과 평가가 같이 쓴다."""
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".pytest_cache",
             ".mypy_cache", ".ruff_cache", ".idea", ".vscode"}
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".html", ".css", ".vue", ".svelte", ".go", ".rs",
            ".java", ".kt", ".rb", ".sh", ".sql"}
DOC_EXT = {".md", ".txt", ".toml", ".json", ".yaml", ".yml", ".cfg", ".ini"}
SKIP_NAMES = {"package-lock.json", "uv.lock", "poetry.lock", "yarn.lock", "pnpm-lock.yaml"}
ENTRY_HINTS = ("main", "app", "server", "index", "api", "routes")
MAX_FILE_LINES = 400
DEFAULT_CHAR_CAP = 60_000


@dataclass
class CodeContext:
    root: Path
    files: list[str]          # 트리에 보일 상대 경로 (텍스트·바이너리 모두)
    key_files: dict[str, str]  # 상대 경로 → 원문 (상한 내 포함된 것만)

    def tree_text(self) -> str:
        return "\n".join(self.files) or "(파일 없음)"

    def key_files_text(self) -> str:
        parts = []
        for rel, text in self.key_files.items():
            lines = text.splitlines()
            shown = lines[:MAX_FILE_LINES]
            body = "\n".join(f"{i:4d}| {line}" for i, line in enumerate(shown, 1))
            more = f"\n… ({len(lines) - MAX_FILE_LINES}줄 생략)" if len(lines) > MAX_FILE_LINES else ""
            parts.append(f"=== {rel} ===\n{body}{more}")
        return "\n\n".join(parts) or "(없음)"

    def identifiers(self) -> set[str]:
        """질문이 '실제 이름'을 담았는지 검사할 때 쓰는 이름 집합: 파일명·확장자 뺀 이름·함수/클래스명."""
        names: set[str] = set()
        for rel in self.files:
            p = Path(rel)
            names.add(p.name)
            if len(p.stem) >= 4:
                names.add(p.stem)
        pat = re.compile(r"(?:def|class|function|async function)\s+([A-Za-z_]\w{2,})|"
                         r"(?:const|let|var)\s+([A-Za-z_]\w{3,})\s*=\s*(?:async\s*)?\(|"
                         r"@app\.(?:get|post|put|delete|patch)\(\s*[\"']([^\"']+)")
        for text in self.key_files.values():
            for m in pat.finditer(text):
                names.update(g for g in m.groups() if g)
        return names

    def excerpt(self, coords: list[str], radius: int = 12, cap: int = 6000) -> str:
        """근거 좌표("파일:줄") 주변 줄을 잘라 채점용 코드 발췌를 만든다."""
        out, used = [], 0
        for c in coords:
            m = re.match(r"^\s*([^:\s]+):(\d+)", c)
            if not m:
                continue
            rel, line = m.group(1), int(m.group(2))
            text = self.read(rel)
            if text is None:
                continue
            lines = text.splitlines()
            lo, hi = max(1, line - radius), min(len(lines), line + radius)
            chunk = f"=== {rel}:{lo}-{hi} ===\n" + "\n".join(f"{i:4d}| {lines[i - 1]}" for i in range(lo, hi + 1))
            if used + len(chunk) > cap:
                break
            out.append(chunk)
            used += len(chunk)
        return "\n\n".join(out) or "(관련 코드 없음)"

    def read(self, rel: str) -> str | None:
        if rel in self.key_files:
            return self.key_files[rel]
        p = (self.root / rel).resolve()
        if not p.is_relative_to(self.root.resolve()) or not p.is_file():
            return None
        try:
            return p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return None


def _list_files(root: Path) -> list[str]:
    try:  # git 저장소면 추적 + 미추적(무시 제외) 파일
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=root, capture_output=True, text=True, check=True).stdout.split("\n")
        files = [f for f in out if f and (root / f).is_file()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        files = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
    return sorted(f for f in files
                  if not any(part in SKIP_DIRS for part in Path(f).parts) and Path(f).name != ".env")


def _priority(rel: str) -> tuple:
    p = Path(rel)
    is_code = p.suffix in CODE_EXT
    is_test = "test" in rel.lower()
    is_entry = p.stem.lower() in ENTRY_HINTS
    return (not is_code, is_test, not is_entry, len(p.parts), rel)


def build(root: Path, char_cap: int = DEFAULT_CHAR_CAP) -> CodeContext:
    root = root.resolve()
    files = _list_files(root)
    candidates = [f for f in files
                  if (Path(f).suffix in CODE_EXT or Path(f).suffix in DOC_EXT) and Path(f).name not in SKIP_NAMES
                  and not f.startswith("data/")]
    key: dict[str, str] = {}
    used = 0
    for rel in sorted(candidates, key=_priority):
        p = root / rel
        if p.stat().st_size > 300_000:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        size = min(len(text), sum(len(line) + 8 for line in text.splitlines()[:MAX_FILE_LINES]))
        if used + size > char_cap:
            continue
        key[rel] = text
        used += size
    return CodeContext(root=root, files=files, key_files=key)


def extract_snapshot(tar_path: Path, dest: Path) -> Path:
    """제출 스냅샷을 한 번만 풀어 둔다 (리허설·평가 공용)."""
    if not dest.exists():
        dest.mkdir(parents=True)
        with tarfile.open(tar_path) as tar:
            tar.extractall(dest, filter="data")
    return dest
