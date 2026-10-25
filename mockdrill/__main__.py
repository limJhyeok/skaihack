"""CLI: python -m mockdrill rehearse --repo <path> [--count 5] [--seconds 180] [--open-book] [--purpose "..."]

터미널에서는 붙여넣기를 막을 수 없다. 답변은 여러 줄로 입력하고 '.'만 있는 줄로 끝낸다.
시간이 끝나면 그때까지 Enter로 입력한 줄까지 제출된다.
"""
import argparse
import os
import select
import sys
import time

from app import llm, rehearsal
from app.config import settings

VERDICT_MARK = {"충족": "●", "부분충족": "◐", "미충족": "○"}


class LineReader:
    """select + os.read로 줄을 읽는다 (파이프 입력에서도 시간 제한이 정확하도록 자체 버퍼 사용)."""

    def __init__(self, fd: int = 0):
        self.fd, self.buf, self.eof = fd, b"", False

    def readline(self, timeout: float | None) -> str | None:
        """한 줄(개행 제외)을 돌려준다. 시간 초과면 None, EOF면 ''(eof 플래그 설정)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while b"\n" not in self.buf:
            if self.eof:
                line, self.buf = self.buf, b""
                return line.decode("utf-8", "replace")
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return None
            ready, _, _ = select.select([self.fd], [], [], left)
            if not ready:
                return None
            chunk = os.read(self.fd, 4096)
            if not chunk:
                self.eof = True
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("utf-8", "replace")


def read_answer(reader: LineReader, seconds: int) -> tuple[str, bool]:
    lines: list[str] = []
    deadline = time.monotonic() + seconds if seconds else None
    while True:
        left = None if deadline is None else deadline - time.monotonic()
        if left is not None and left <= 0:
            return "\n".join(lines), True
        line = reader.readline(left)
        if line is None:
            return "\n".join(lines), True
        if line.strip() == ".":
            return "\n".join(lines).strip(), False
        if line == "" and reader.eof and not reader.buf:  # 입력 끝
            return "\n".join(lines).strip(), False
        lines.append(line)


def print_results(r: dict) -> None:
    sc = r["scores"]
    print("\n" + "=" * 60)
    print(f"인지 관리 점수: {sc['total']} / 100   " +
          "  ".join(f"{v}: {n}" for v, n in sc["verdict_counts"].items()))
    for cat in sc["by_category"].values():
        print(f"  - {cat['label']}: {cat['score'] if cat['score'] is not None else '문항 없음'} ({cat['questions']}문항)")
    for q in r["questions"] + r["followups"]:
        g, ma = q["grade"], q["model_answer"]
        tag = "[재심문] " if q["followup"] else ""
        print("\n" + "-" * 60)
        print(f"{tag}{q['id']} [{q['type']} / {rehearsal.CATEGORIES[q['category']]}] {VERDICT_MARK[g['verdict']]} {g['verdict']}")
        print(f"Q. {q['question']}")
        print(f"내 답 ({q['elapsed_sec']}초{', 시간 초과' if q['timed_out'] else ''}): {q['answer'] or '(빈 답)'}")
        print("단계 판정: " + ", ".join(f"{k}={v}" for k, v in g["steps"].items()))
        for step, label in (("weak_point", "약점"), ("impact", "파급"), ("plan", "계획")):
            print(f"  모범 {label}: {ma[step]['text']}  [{', '.join(ma[step]['evidence'])}]")
        if g["misconception"]:
            print(f"  오해: {g['misconception']}")
        if g["revisit"]:
            print(f"  다시 볼 곳: {', '.join(g['revisit'])}")
        print(f"  피드백: {g['comment']}")
    print(f"\n저장: {settings.data_dir / 'rehearsals' / r['rehearsal_id'] / 'rehearsal.json'}")


def run_questions(rid: str, reader: LineReader) -> None:
    while (q := rehearsal.next_question(rid)) is not None:
        limit = q["remaining_sec"]
        print("\n" + "=" * 60)
        print(f"문항 {q['index']}/{q['total']}  ({'제한 없음' if limit is None else f'{limit}초'})")
        print(q["question"])
        print("> 답변 입력 후 '.'만 있는 줄로 끝내세요.")
        text, timed_out = read_answer(reader, limit or 0)
        if timed_out:
            print("\n[시간 초과 — 지금까지 입력한 내용으로 제출합니다]")
        rehearsal.answer(rid, q["id"], text, timed_out)


def cmd_rehearse(args) -> int:
    if not settings.model_strong:
        print("MOCK_MODEL_STRONG 환경변수가 필요합니다.", file=sys.stderr)
        return 2
    mode = "open_book" if args.open_book else "closed_book"
    seconds = args.seconds if not (args.open_book and args.seconds == 0) else 0
    if mode == "closed_book":
        print("closed book: 리허설 중에는 코드를 보지 않습니다. (시스템이 강제하지 않습니다)")
    print(f"출제 중… ({args.repo}, {args.count}문항)")
    try:
        r = rehearsal.create_standalone(args.repo, args.purpose, mode, args.count, seconds)
        reader = LineReader()
        run_questions(r["rehearsal_id"], reader)
        print("\n채점 중…")
        r = rehearsal.grade(r["rehearsal_id"])
        if rehearsal.can_followup(r) and not args.no_followup:
            print("\n미충족이 2문항 이상입니다. 재심문을 진행합니다.")
            rehearsal.create_followup(r["rehearsal_id"])
            run_questions(r["rehearsal_id"], reader)
            print("\n채점 중…")
            r = rehearsal.grade(r["rehearsal_id"])
    except (rehearsal.RehearsalError, llm.LLMError) as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    print_results(r)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m mockdrill")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rehearse", help="임의 저장소로 설명 리허설")
    r.add_argument("--repo", required=True)
    r.add_argument("--count", type=int, default=settings.rehearsal_count)
    r.add_argument("--seconds", type=int, default=settings.rehearsal_sec, help="문항당 초. --open-book과 0이면 제한 없음")
    r.add_argument("--open-book", action="store_true")
    r.add_argument("--purpose", default="", help="저장소 목적 2~3줄 (선택)")
    r.add_argument("--no-followup", action="store_true", help="재심문 생략")
    args = p.parse_args()
    return cmd_rehearse(args)


if __name__ == "__main__":
    sys.exit(main())
