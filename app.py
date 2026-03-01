# app.py (Streamlit UI)
import streamlit as st
import requests
import pandas as pd
from datetime import datetime

DEFAULT_BACKEND = "http://127.0.0.1:8000"
st.set_page_config(page_title="AdaptiveLearn (Streamlit)", layout="wide")

def api_post(path, payload, backend=DEFAULT_BACKEND, timeout=60):
    try:
        r = requests.post(f"{backend}{path}", json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

def api_get(path, params=None, backend=DEFAULT_BACKEND, timeout=20):
    try:
        r = requests.get(f"{backend}{path}", params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}

# Sidebar
backend = st.sidebar.text_input("Backend URL", DEFAULT_BACKEND)

st.sidebar.markdown("---")
st.sidebar.header("Auth")

if "user" not in st.session_state:
    st.session_state["user"] = None
    st.session_state["role"] = None
if "current_q" not in st.session_state:
    st.session_state["current_q"] = None

with st.sidebar.form("auth"):
    name = st.text_input("Name")
    pwd = st.text_input("Password", type="password")
    role_choice = st.selectbox("Role for registration", ["teacher", "student"])
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.form_submit_button("Register"):
            res = api_post("/register", {"name": name, "password": pwd, "role": role_choice}, backend=backend)
            if res.get("ok"):
                st.success("Registered. Please login.")
            else:
                st.error(res)
    with col2:
        if st.form_submit_button("Login"):
            res = api_post("/login", {"name": name, "password": pwd}, backend=backend)
            if res.get("ok"):
                st.session_state["user"] = res["user"]
                st.session_state["role"] = res["role"]
                st.success(f"Logged in as {res['user']} ({res['role']})")
            else:
                st.error(res)
    with col3:
        if st.form_submit_button("Logout"):
            st.session_state["user"] = None
            st.session_state["role"] = None
            st.session_state["current_q"] = None
            st.success("Logged out")

st.sidebar.write("Current:", st.session_state.get("user") or "—")
st.sidebar.write("Role:", st.session_state.get("role") or "—")

# Main
st.title("AdaptiveLearn — Personalized practice (Ollama + Streamlit)")
tabs = st.tabs(["Generate", "Manage Questions", "Answer", "Progress", "Scheduler", "Health"])

# Generate
with tabs[0]:
    st.header("Generate (teachers only)")
    if st.session_state.get("role") != "teacher":
        st.info("Login as a teacher to generate items.")
    topic = st.text_input("Topic", "Introduction to Machine Learning")
    num_q = st.number_input("How many?", min_value=1, max_value=20, value=3, step=1)
    mcq = st.checkbox("Multiple-choice (MCQ)", value=False)
    choices = st.number_input("Choices per MCQ", min_value=2, max_value=8, value=4, step=1) if mcq else 4

    if st.button("Generate now"):
        if st.session_state.get("role") != "teacher":
            st.error("Only teachers can generate.")
        else:
            payload = {
                "user": st.session_state.get("user") or "teacher",
                "topic": topic,
                "num_questions": int(num_q),
                "mcq": bool(mcq),
                "choices_count": int(choices),
            }
            with st.spinner("Generating..."):
                res = api_post("/generate_assessment", payload, backend=backend, timeout=90)
            if res.get("ok"):
                st.success(f"Created {len(res['created_ids'])} questions.")
                st.caption("Raw model preview (first 600 chars)")
                st.code(res.get("raw_preview", ""))
            else:
                st.error(res)

# Manage
with tabs[1]:
    st.header("Question bank")
    data = api_get("/questions", backend=backend)
    if isinstance(data, list) and data:
        rows = []
        for q in data:
            if q.get("qtype") == "mcq":
                rows.append({
                    "id": q["id"], "topic": q["topic"], "type": "MCQ",
                    "prompt": q["prompt"], "options": "; ".join(q.get("options") or []),
                    "correct": q.get("correct"),
                })
            else:
                rows.append({
                    "id": q["id"], "topic": q["topic"], "type": "Short",
                    "prompt": q["prompt"], "answer_key": q.get("answer_key", "")
                })
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True)
        del_id = st.number_input("Delete by id", min_value=1, value=int(df["id"].iloc[0]), step=1)
        if st.button("Delete"):
            try:
                dres = requests.delete(f"{backend}/question/{int(del_id)}", timeout=10).json()
                if dres.get("ok"):
                    st.success(dres)
                else:
                    st.error(dres)
            except Exception as e:
                st.error(str(e))
    else:
        st.info("No questions yet. Generate some.")

# Answer
with tabs[2]:
    st.header("Answer a question")
    qid = st.number_input("Question id", min_value=1, value=1, step=1)
    if st.button("Load question"):
        res = api_get(f"/question/{int(qid)}", backend=backend)
        st.session_state["current_q"] = res if isinstance(res, dict) and res.get("id") else None

    q = st.session_state.get("current_q")
    if q:
        st.subheader(f"Q{q['id']} — {q['topic']}")
        st.write(q["prompt"])
        if q.get("qtype") == "mcq":
            opts = q.get("options") or []
            for i, o in enumerate(opts):
                st.write(f"{chr(65+i)}. {o}")
            ans = st.text_input("Your answer (A/B/C/... or full text)")
        else:
            ans = st.text_area("Your answer")
        if st.button("Submit"):
            if not ans.strip():
                st.error("Answer is empty.")
            else:
                payload = {
                    "user": st.session_state.get("user") or "guest",
                    "question_id": int(q["id"]),
                    "answer": ans,
                }
                res = api_post("/submit_answer", payload, backend=backend)
                if res.get("ok"):
                    st.success(f"Attempt queued. id={res['attempt_id']}")
                else:
                    st.error(res)

# Progress
with tabs[3]:
    st.header("Progress")
    who = st.text_input("User", value=st.session_state.get("user") or "guest")
    if st.button("Load progress"):
        res = api_get("/progress", params={"user": who}, backend=backend)
        atts = res.get("attempts", [])
        if atts:
            df = pd.DataFrame(atts)
            df["created_at"] = df["created_at"].apply(
                lambda t: datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")
            )
            st.dataframe(df, use_container_width=True)
            st.line_chart(df["score"].fillna(0))
        else:
            st.info("No attempts yet.")

# Scheduler
with tabs[4]:
    st.header("Spaced repetition")
    who = st.text_input("User for schedule", value=st.session_state.get("user") or "guest", key="sched_user")
    if st.button("Fetch due"):
        res = api_get("/spaced_due", params={"user": who}, backend=backend)
        due = res.get("due", [])
        if due:
            for d in due:
                st.write(f"{d['topic']} — level {d['level']} — next_due {datetime.fromtimestamp(d['next_due']).date()}")
        else:
            st.info("Nothing due right now.")

# Health
with tabs[5]:
    st.header("Health")
    if st.button("Check backend"):
        st.json(api_get("/health", backend=backend))
    st.caption("Make sure Ollama is running (http://127.0.0.1:11434) and the model is pulled (e.g., `ollama run llama3.2:1b`).")
