# migrate_db.py
import sqlite3

DB = "adaptive_learning.db"
conn = sqlite3.connect(DB)
cur = conn.cursor()

def ensure(table: str, coldef: str):
    col = coldef.split()[0]
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        print(f"Adding {col} to {table}")
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
        conn.commit()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    password_hash TEXT,
    salt TEXT,
    role TEXT DEFAULT 'student'
);
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT,
    prompt TEXT,
    answer_key TEXT,
    qtype TEXT DEFAULT 'short',
    options TEXT,
    correct TEXT
);
""")
cur.execute("""
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
""")
cur.execute("""
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
conn.commit()

ensure("users", "password_hash TEXT")
ensure("users", "salt TEXT")
ensure("users", "role TEXT DEFAULT 'student'")
ensure("questions", "qtype TEXT DEFAULT 'short'")
ensure("questions", "options TEXT")
ensure("questions", "correct TEXT")
ensure("attempts", "status TEXT DEFAULT 'pending'")

print("Migration complete.")
conn.close()
