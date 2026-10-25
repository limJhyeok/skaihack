"""작은 메모 요약 API (리허설 테스트용 예제 저장소)."""
import os
import sqlite3

from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel

app = FastAPI()
DB_PATH = os.environ.get("MEMO_DB", "memos.db")
client = OpenAI()


class MemoIn(BaseModel):
    author: str
    text: str


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS memos (id INTEGER PRIMARY KEY, author TEXT, text TEXT, summary TEXT)")
    return conn


def summarize(text: str) -> str:
    r = client.chat.completions.create(
        model=os.environ.get("SUMMARY_MODEL", "gpt-4o-mini"),
        messages=[{"role": "user", "content": f"다음 메모를 한 줄로 요약해줘:\n{text}"}],
    )
    return r.choices[0].message.content


@app.post("/memos")
def create_memo(memo: MemoIn):
    if len(memo.text) > 5000:
        raise HTTPException(400, "메모가 너무 깁니다")
    summary = summarize(memo.text)
    conn = get_db()
    cur = conn.execute("INSERT INTO memos (author, text, summary) VALUES (?, ?, ?)", (memo.author, memo.text, summary))
    conn.commit()
    return {"id": cur.lastrowid, "summary": summary}


@app.get("/memos/{memo_id}")
def read_memo(memo_id: int):
    row = get_db().execute("SELECT id, author, text, summary FROM memos WHERE id = ?", (memo_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "없는 메모")
    return dict(zip(["id", "author", "text", "summary"], row))


@app.get("/search")
def search(q: str):
    rows = get_db().execute(f"SELECT id, summary FROM memos WHERE summary LIKE '%{q}%'").fetchall()
    return [{"id": r[0], "summary": r[1]} for r in rows]
