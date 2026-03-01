# backend.py
import os
import sqlite3
import logging
import time
import json
import hashlib
import binascii
import threading
import queue
from typing import List, Optional, Dict, Any

import httpx
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("adaptive")

# -------------------- Config --------------------
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:1b")
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.0"))
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
DB_PATH = os.getenv("DB_PATH", "adaptive_learning.db")

# -------------------- DB --------------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = threading.Lock()

def execscript(sql: str):
    with db_lock:
        conn.executescript(sql)
        conn.commit()

execscript("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    password_hash TEXT,
    salt TEXT,
    role TEXT DEFAULT 'student'
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT,
    prompt TEXT,
    answer_key TEXT,
    qtype TEXT DEFAULT 'short',
    options TEXT,
    correct TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    question_id INTEGER,
    answer TEXT,
    score INTEGER,
    feedback TEXT,
    recommendation TEXT,
    status TEXT DEFAULT 'pending',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS mastery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    topic TEXT,
    level INTEGER,
    interval_days INTEGER,
    last_review INTEGER,
    next_due INTEGER
);
""")

def ensure_columns():
    def cols(table: str) -> List[str]:
        with db_lock:
            r = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [row[1] for row in r]

    needed = {
        "users": ["password_hash", "salt", "role"],
        "questions": ["qtype", "options", "correct"],
        "attempts": ["status"],
    }
    for t, want in needed.items():
        have = cols(t)
        with db_lock:
            for c in want:
                if c not in have:
                    default_sql = ""
                    if t == "users" and c == "role":
                        default_sql = " TEXT DEFAULT 'student'"
                    elif t == "attempts" and c == "status":
                        default_sql = " TEXT DEFAULT 'pending'"
                    else:
                        default_sql = " TEXT"
                    conn.execute(f"ALTER TABLE {t} ADD COLUMN {c}{default_sql}")
            conn.commit()

ensure_columns()

# -------------------- Auth helpers --------------------
def gen_salt(n=16) -> str:
    return binascii.hexlify(os.urandom(n)).decode()

def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

def register_user(name: str, password: str, role: str) -> int:
    with db_lock:
        r = conn.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()
        if r:
            raise HTTPException(400, "User exists")
        salt = gen_salt()
        ph = hash_password(password, salt)
        conn.execute(
            "INSERT INTO users (name, password_hash, salt, role) VALUES (?,?,?,?)",
            (name, ph, salt, role),
        )
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()["id"]
        return uid

def verify_user(name: str, password: str) -> bool:
    with db_lock:
        r = conn.execute("SELECT password_hash, salt FROM users WHERE name=?", (name,)).fetchone()
    if not r:
        return False
    return r["password_hash"] == hash_password(password, r["salt"])

def user_id_by_name(name: str) -> Optional[int]:
    with db_lock:
        r = conn.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()
    return r["id"] if r else None

def user_role(name: str) -> Optional[str]:
    with db_lock:
        r = conn.execute("SELECT role FROM users WHERE name=?", (name,)).fetchone()
    return r["role"] if r else None

def get_or_create_user(name: str, role: str = "student") -> int:
    uid = user_id_by_name(name)
    if uid:
        return uid
    return register_user(name, gen_salt()[:8], role)

# -------------------- Pydantic models --------------------
class RegisterReq(BaseModel):
    name: str
    password: str
    role: str = "student"

class LoginReq(BaseModel):
    name: str
    password: str

class GenerateReq(BaseModel):
    user: str
    topic: str
    num_questions: int = 3
    mcq: bool = False
    choices_count: int = 4

class SubmitReq(BaseModel):
    user: str
    question_id: int
    answer: str

# -------------------- LLM helpers --------------------
async def ollama_generate(prompt: str, temperature: float = None) -> str:
    url = f"{OLLAMA_HOST}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": (OLLAMA_TEMPERATURE if temperature is None else temperature)},
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
        return (data.get("response") or "").strip()

def ollama_generate_sync(prompt: str, temperature: float = None) -> str:
    url = f"{OLLAMA_HOST}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": (OLLAMA_TEMPERATURE if temperature is None else temperature)},
    }
    r = requests.post(url, json=payload, timeout=60)
    r.raise_for_status()
    return (r.json().get("response") or "").strip()

async def gemini_rewrite_simple(text: str) -> Optional[str]:
    if not GEMINI_KEY:
        return None
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    headers = {"Content-Type": "application/json", "X-goog-api-key": GEMINI_KEY}
    body = {"contents": [{"parts": [{"text": text}]}]}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            return data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
    except Exception as e:
        log.warning("Gemini error: %s", e)
        return None

def extract_json_blocks(text: str) -> List[str]:
    import re
    blocks = []
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, flags=re.S):
        blocks.append(m.group(1))
    if not blocks:
        m = re.search(r"(\[.*\]|\{.*\})", text, flags=re.S)
        if m:
            blocks.append(m.group(1))
    return blocks

# -------------------- Background grading --------------------
grade_queue: "queue.Queue[int]" = queue.Queue()
worker_started = False

def schedule_levels() -> List[int]:
    return [1, 2, 4, 7, 15]  # days

def update_mastery(uid: int, topic: str, correct: bool):
    now = int(time.time())
    with db_lock:
        r = conn.execute(
            "SELECT * FROM mastery WHERE user_id=? AND topic=?", (uid, topic)
        ).fetchone()
        if not r:
            level = 1 if correct else 0
            interval = schedule_levels()[0] if correct else 1
            next_due = now + interval * 86400
            conn.execute(
                "INSERT INTO mastery (user_id, topic, level, interval_days, last_review, next_due) "
                "VALUES (?,?,?,?,?,?)",
                (uid, topic, level, interval, now, next_due),
            )
        else:
            level = r["level"]
            if correct:
                level = min(level + 1, len(schedule_levels()))
            else:
                level = max(level - 1, 0)
            interval = schedule_levels()[max(level - 1, 0)] if level > 0 else 1
            next_due = now + interval * 86400
            conn.execute(
                "UPDATE mastery SET level=?, interval_days=?, last_review=?, next_due=? WHERE id=?",
                (level, interval, now, next_due, r["id"]),
            )
        conn.commit()

def grade_attempt_sync(attempt_id: int):
    with db_lock:
        a = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if not a or a["status"] != "queued":
        return
    with db_lock:
        q = conn.execute("SELECT * FROM questions WHERE id=?", (a["question_id"],)).fetchone()
    if not q:
        with db_lock:
            conn.execute(
                "UPDATE attempts SET status='error', feedback=? WHERE id=?",
                ("Question not found", attempt_id),
            )
            conn.commit()
        return

    user_answer = (a["answer"] or "").strip()
    topic = q["topic"]
    score, feedback = 0, ""
    recommendation = ""

    if q["qtype"] == "mcq":
        options = json.loads(q["options"] or "[]")
        correct = (q["correct"] or "").strip().lower()
        ans = user_answer.strip()
        is_correct = False
        if len(ans) == 1 and ans.upper().isalpha():
            idx = ord(ans.upper()) - 65
            if 0 <= idx < len(options):
                is_correct = (options[idx].strip().lower() == correct)
        else:
            is_correct = (ans.strip().lower() == correct)
        score = 1 if is_correct else 0
        feedback = "Correct!" if is_correct else f"Not quite. Correct answer: {q['correct']}"
    else:
        ref = (q["answer_key"] or "").strip()
        prompt = (
            "You are grading a short answer. Return strictly a JSON object with keys "
            "'score' (0 or 1) and 'feedback' (one concise sentence). "
            f"Question: {q['prompt']}\n"
            f"Reference answer: {ref}\n"
            f"Learner answer: {user_answer}\n"
            "Consider it correct if the essential idea matches the reference."
        )
        try:
            text = ollama_generate_sync(prompt, temperature=0.0)
        except Exception as e:
            text = f'{{"score":0,"feedback":"Model error: {e}"}}'

        parsed = None
        for block in extract_json_blocks(text) or [text]:
            try:
                parsed = json.loads(block)
                break
            except Exception:
                continue
        if isinstance(parsed, dict):
            score = int(parsed.get("score", 0))
            feedback = str(parsed.get("feedback", ""))[:500]
        else:
            score = 1 if ref and user_answer and user_answer.lower() in ref.lower() else 0
            feedback = "Auto-graded heuristically."

    recommendation = "Advance to the next item." if score == 1 else "Review and try an easier step."

    # Optional Gemini rephrase (non-blocking best-effort)
    if GEMINI_KEY and feedback:
        try:
            # quick sync call using requests (still best-effort)
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
            headers = {"Content-Type": "application/json", "X-goog-api-key": GEMINI_KEY}
            body = {"contents": [{"parts": [{"text": f"Rewrite simply for a student: {feedback}"}]}]}
            r = requests.post(url, headers=headers, json=body, timeout=10)
            if r.ok:
                gd = r.json()
                gtxt = gd.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text")
                if gtxt:
                    feedback = gtxt
        except Exception as e:
            log.info("Gemini skip: %s", e)

    with db_lock:
        conn.execute(
            "UPDATE attempts SET score=?, feedback=?, recommendation=?, status='graded' WHERE id=?",
            (score, feedback, recommendation, attempt_id),
        )
        conn.commit()

    uid = a["user_id"]
    update_mastery(uid, topic, correct=(score == 1))

def worker_loop(qobj: "queue.Queue[int]"):
    log.info("Background grading worker started.")
    while True:
        try:
            aid = qobj.get()
            if aid is None:
                break
            grade_attempt_sync(aid)
        except Exception as e:
            log.exception("Worker error: %s", e)

# -------------------- FastAPI --------------------
app = FastAPI(title="Adaptive Learn API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_worker_thread: Optional[threading.Thread] = None

@app.on_event("startup")
def _startup():
    global worker_started, _worker_thread
    if not worker_started:
        _worker_thread = threading.Thread(target=worker_loop, args=(grade_queue,), daemon=True)
        _worker_thread.start()
        worker_started = True

# -------------------- Routes --------------------
@app.get("/health")
def health():
    ok = False
    try:
        r = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=5)
        ok = r.status_code == 200
    except Exception:
        ok = False
    return {"ok": ok, "ollama": OLLAMA_HOST, "model": OLLAMA_MODEL}

@app.post("/register")
def api_register(req: RegisterReq):
    uid = register_user(req.name, req.password, req.role)
    return {"ok": True, "user_id": uid}

@app.post("/login")
def api_login(req: LoginReq):
    if not verify_user(req.name, req.password):
        raise HTTPException(401, "Invalid credentials")
    return {"ok": True, "user": req.name, "role": user_role(req.name)}

@app.get("/questions")
def list_questions():
    with db_lock:
        rows = conn.execute("SELECT * FROM questions ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        item = dict(r)
        if item.get("options"):
            try:
                item["options"] = json.loads(item["options"])
            except Exception:
                item["options"] = []
        out.append(item)
    return out

@app.get("/question/{qid}")
def get_question(qid: int):
    with db_lock:
        r = conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
    if not r:
        raise HTTPException(404, "Not found")
    item = dict(r)
    if item.get("options"):
        try:
            item["options"] = json.loads(item["options"])
        except Exception:
            item["options"] = []
    return item

@app.delete("/question/{qid}")
def delete_question(qid: int):
    with db_lock:
        conn.execute("DELETE FROM questions WHERE id=?", (qid,))
        conn.commit()
    return {"ok": True, "deleted": qid}

@app.post("/generate_assessment")
async def generate_assessment(req: GenerateReq):
    if user_role(req.user) != "teacher":
        raise HTTPException(403, "Only teacher can generate")

    if req.mcq:
        prompt = (
            f"Generate {req.num_questions} multiple-choice questions on '{req.topic}'. "
            f"Return JSON list ONLY, where each item has:\n"
            f"  'prompt' (string),\n"
            f"  'options' (array of {req.choices_count} plausible distinct answers),\n"
            f"  'correct' (the single correct option exactly as in 'options')."
        )
        temp = 0.2
    else:
        prompt = (
            f"Generate {req.num_questions} short-answer assessment questions on '{req.topic}'. "
            f"Return JSON list ONLY, each item with: 'prompt' (string), 'answer_key' (string)."
        )
        temp = 0.0

    raw = await ollama_generate(prompt, temperature=temp)

    data = None
    for block in extract_json_blocks(raw) or [raw]:
        try:
            parsed = json.loads(block)
            if isinstance(parsed, list):
                data = parsed
                break
        except Exception:
            continue
    if not data:
        raise HTTPException(500, f"Model did not return valid JSON. Raw: {raw[:400]}")

    created_ids = []
    with db_lock:
        for item in data:
            topic = req.topic
            prompt_text = str(item.get("prompt", "")).strip()
            if req.mcq:
                options = item.get("options") or []
                correct = item.get("correct") or ""
                conn.execute(
                    "INSERT INTO questions (topic, prompt, qtype, options, correct) VALUES (?,?,?,?,?)",
                    (topic, prompt_text, "mcq", json.dumps(options), correct),
                )
            else:
                answer_key = str(item.get("answer_key", "")).strip()
                conn.execute(
                    "INSERT INTO questions (topic, prompt, answer_key, qtype) VALUES (?,?,?,?)",
                    (topic, prompt_text, answer_key, "short"),
                )
            created_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()

    return {"ok": True, "created_ids": created_ids, "raw_preview": raw[:600]}

@app.post("/submit_answer")
def submit_answer(req: SubmitReq):
    uid = get_or_create_user(req.user, "student")
    now = int(time.time())
    with db_lock:
        conn.execute(
            "INSERT INTO attempts (user_id, question_id, answer, status, created_at) VALUES (?,?,?,?,?)",
            (uid, req.question_id, req.answer, "queued", now),
        )
        conn.commit()
        attempt_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    grade_queue.put(attempt_id)
    return {"ok": True, "attempt_id": attempt_id}

@app.get("/attempt/{attempt_id}")
def get_attempt(attempt_id: int):
    with db_lock:
        r = conn.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Not found")
    return dict(r)

@app.get("/progress")
def progress(user: str):
    uid = user_id_by_name(user)
    if not uid:
        return {"attempts": []}
    with db_lock:
        rows = conn.execute(
            "SELECT a.id AS attempt_id, a.question_id, a.answer, a.score, a.feedback, "
            "a.recommendation, a.status, a.created_at, q.topic, q.prompt "
            "FROM attempts a JOIN questions q ON a.question_id=q.id "
            "WHERE a.user_id=? ORDER BY a.id DESC",
            (uid,),
        ).fetchall()
    return {"attempts": [dict(r) for r in rows]}

@app.get("/spaced_due")
def spaced_due(user: str):
    uid = user_id_by_name(user)
    if not uid:
        return {"due": []}
    now = int(time.time())
    with db_lock:
        rows = conn.execute(
            "SELECT * FROM mastery WHERE user_id=? AND next_due<=? ORDER BY next_due ASC",
            (uid, now),
        ).fetchall()
    return {"due": [dict(r) for r in rows]}
