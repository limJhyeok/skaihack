"""Helper (5.4): 모의 플랫폼 사용법만 안내."""
from . import llm, prompts, sessions
from .config import settings

HELPER = "helper_chat.jsonl"


def send(session_id: str, text: str) -> dict:
    s = sessions.load_session(session_id)
    log = sessions.read_jsonl(session_id, HELPER)
    if not (log and log[-1]["role"] == "user" and log[-1]["text"] == text):
        rec = {"ts": sessions.now_iso(), "elapsed_sec": sessions.elapsed_sec(s), "role": "user", "text": text}
        sessions.append_jsonl(session_id, HELPER, rec)
        log.append(rec)
    system = prompts.load("helper_system", workspace_path=s.workspace_path,
                          rehearsal_count=settings.rehearsal_count, rehearsal_sec=settings.rehearsal_sec)
    messages = [{"role": "system", "content": system}] + [
        {"role": "user" if r["role"] == "user" else "assistant", "content": r["text"]} for r in log
    ]
    reply = llm.chat(model=settings.model_fast, messages=messages, purpose="helper")
    out = {"ts": sessions.now_iso(), "elapsed_sec": sessions.elapsed_sec(s), "role": "helper", "text": reply}
    sessions.append_jsonl(session_id, HELPER, out)
    return out


def log(session_id: str) -> list[dict]:
    return sessions.read_jsonl(session_id, HELPER)
