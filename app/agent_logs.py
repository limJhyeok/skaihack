"""코딩 에이전트 세션 로그 어댑터 (5.6).

형식은 실제 파일로 확인함 (codex-cli 0.160.0, Claude Code 2.1.289, 2026-10-24):
- Codex: <root>/YYYY/MM/DD/rollout-*.jsonl. 첫 줄 session_meta.payload.cwd.
  사용자 프롬프트 = event_msg/item_completed 중 item.type == "UserMessage".
  토큰 = 마지막 event_msg/token_count.info.total_token_usage (누적값).
  편집 파일 = item_completed 중 item.type == "FileChange"의 changes 키.
- Claude Code: <root>/<인코딩된 경로>/<session>.jsonl. 각 줄에 cwd.
  사용자 프롬프트 = type "user", isMeta 아님, content가 문자열(또는 text 블록).
  토큰 = assistant message.usage를 message.id로 중복 제거 후 합산.
"""
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol


MSG_CAP = 800  # 에이전트 메시지 1개당 보관 글자 수


@dataclass
class AgentTrace:
    tool: str
    path: str
    session_id: str
    started_at: str | None
    ended_at: str | None
    user_prompts: list[dict] = field(default_factory=list)  # [{ts, text}]
    agent_messages: list[dict] = field(default_factory=list)  # [{ts, text}] 에이전트의 답변·제안 (리허설 ai_decision 문항 근거)
    tool_calls_count: int = 0
    files_edited: list[str] = field(default_factory=list)
    # [{ts, billable}] 누적 billable 토큰의 시간 흐름 (토큰 급증 구간 감지용)
    token_timeline: list[dict] = field(default_factory=list)
    # {input, cached_input, output, total, billable}. billable = 캐시 제외 입력 + 출력 (Codex "tokens used"와 같은 값)
    token_usage: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class AgentLogAdapter(Protocol):
    name: str

    def find_sessions(self, workspace: Path, start: datetime, end: datetime) -> list[Path]: ...

    def parse(self, path: Path) -> AgentTrace: ...


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _inside(cwd: str | None, workspace: Path) -> bool:
    if not cwd:
        return False
    try:
        return Path(cwd.removeprefix("file://")).resolve().is_relative_to(workspace.resolve())
    except (OSError, ValueError):
        return False


def _read(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _overlaps(first: datetime | None, last: datetime | None, start: datetime, end: datetime) -> bool:
    return first is not None and last is not None and first <= end and last >= start


class CodexAdapter:
    name = "codex"

    def __init__(self, root: Path):
        self.root = root

    def find_sessions(self, workspace: Path, start: datetime, end: datetime) -> list[Path]:
        out = []
        if not self.root.is_dir():
            return out
        for p in self.root.rglob("rollout-*.jsonl"):
            if datetime.fromtimestamp(p.stat().st_mtime).astimezone() < start:
                continue
            first = next(_read(p), None)
            if not first or first.get("type") != "session_meta":
                continue
            if not _inside(first["payload"].get("cwd"), workspace):
                continue
            t = self.parse(p)
            if _overlaps(_ts(t.started_at), _ts(t.ended_at), start, end):
                out.append(p)
        return sorted(out, key=lambda p: self.parse(p).started_at or "")

    def parse(self, path: Path) -> AgentTrace:
        t = AgentTrace(tool=self.name, path=str(path), session_id="", started_at=None, ended_at=None)
        files: set[str] = set()
        usage = None
        for r in _read(path):
            ts = r.get("timestamp")
            t.started_at = t.started_at or ts
            t.ended_at = ts or t.ended_at
            p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
            if r.get("type") == "session_meta":
                t.session_id = p.get("id") or p.get("session_id") or ""
            elif r.get("type") == "response_item" and p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
                t.tool_calls_count += 1
            elif r.get("type") == "event_msg" and p.get("type") == "item_completed":
                item = p.get("item") or {}
                if item.get("type") == "UserMessage":
                    text = "".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict))
                    t.user_prompts.append({"ts": ts, "text": text})
                elif item.get("type") == "AgentMessage":
                    text = "".join(c.get("text", "") for c in item.get("content", []) if isinstance(c, dict))
                    if text.strip():
                        t.agent_messages.append({"ts": ts, "text": text[:MSG_CAP]})
                elif item.get("type") == "FileChange":
                    files.update((item.get("changes") or {}).keys())
            elif r.get("type") == "event_msg" and p.get("type") == "token_count":
                usage = ((p.get("info") or {}).get("total_token_usage")) or usage
                if usage:
                    t.token_timeline.append({"ts": ts, "billable": usage.get("input_tokens", 0)
                                             - usage.get("cached_input_tokens", 0) + usage.get("output_tokens", 0)})
        t.files_edited = sorted(files)
        if usage:
            inp, cached, out = usage.get("input_tokens", 0), usage.get("cached_input_tokens", 0), usage.get("output_tokens", 0)
            t.token_usage = {"input": inp, "cached_input": cached, "output": out,
                             "total": usage.get("total_tokens", inp + out), "billable": inp - cached + out}
        return t


EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


class ClaudeCodeAdapter:
    name = "claude_code"

    def __init__(self, root: Path):
        self.root = root

    def find_sessions(self, workspace: Path, start: datetime, end: datetime) -> list[Path]:
        out = []
        if not self.root.is_dir():
            return out
        for p in self.root.glob("*/*.jsonl"):
            if datetime.fromtimestamp(p.stat().st_mtime).astimezone() < start:
                continue
            if not any(_inside(r.get("cwd"), workspace) for r in _read(p)):
                continue
            t = self.parse(p)
            if _overlaps(_ts(t.started_at), _ts(t.ended_at), start, end):
                out.append(p)
        return sorted(out, key=lambda p: self.parse(p).started_at or "")

    def parse(self, path: Path) -> AgentTrace:
        t = AgentTrace(tool=self.name, path=str(path), session_id=path.stem, started_at=None, ended_at=None)
        files: set[str] = set()
        usage_by_msg: dict[str, dict] = {}
        tool_ids: set[str] = set()
        for r in _read(path):
            ts = r.get("timestamp")
            if ts:
                t.started_at = t.started_at or ts
                t.ended_at = ts
            m = r.get("message") or {}
            content = m.get("content")
            if r.get("type") == "user" and not r.get("isMeta"):
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list) and all(c.get("type") == "text" for c in content):
                    text = "".join(c.get("text", "") for c in content)
                else:
                    continue  # tool_result 등
                if text.startswith("[Request interrupted") or text.startswith("<local-command"):
                    continue
                t.user_prompts.append({"ts": ts, "text": text})
            elif r.get("type") == "assistant":
                if m.get("id") and m.get("usage") and m["id"] not in usage_by_msg:
                    usage_by_msg[m["id"]] = u = m["usage"]
                    prev = t.token_timeline[-1]["billable"] if t.token_timeline else 0
                    t.token_timeline.append({"ts": ts, "billable": prev + u.get("input_tokens", 0)
                                             + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0)})
                for c in content if isinstance(content, list) else []:
                    if c.get("type") == "text" and c.get("text", "").strip():
                        t.agent_messages.append({"ts": ts, "text": c["text"][:MSG_CAP]})
                    if c.get("type") == "tool_use" and c.get("id") not in tool_ids:
                        tool_ids.add(c.get("id"))
                        if c.get("name") in EDIT_TOOLS:
                            fp = (c.get("input") or {}).get("file_path") or (c.get("input") or {}).get("notebook_path")
                            if fp:
                                files.add(fp)
        t.tool_calls_count = len(tool_ids)
        t.files_edited = sorted(files)
        if usage_by_msg:
            inp = sum(u.get("input_tokens", 0) for u in usage_by_msg.values())
            cread = sum(u.get("cache_read_input_tokens", 0) for u in usage_by_msg.values())
            cwrite = sum(u.get("cache_creation_input_tokens", 0) for u in usage_by_msg.values())
            out = sum(u.get("output_tokens", 0) for u in usage_by_msg.values())
            t.token_usage = {"input": inp + cread + cwrite, "cached_input": cread, "output": out,
                             "total": inp + cread + cwrite + out, "billable": inp + cwrite + out}
        return t


def default_paths() -> dict:
    codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    return {"codex_sessions_dir": str(codex_home / "sessions"),
            "claude_projects_dir": str(Path("~/.claude/projects").expanduser())}


def adapters(paths: dict) -> list[AgentLogAdapter]:
    return [CodexAdapter(Path(paths["codex_sessions_dir"]).expanduser()),
            ClaudeCodeAdapter(Path(paths["claude_projects_dir"]).expanduser())]
