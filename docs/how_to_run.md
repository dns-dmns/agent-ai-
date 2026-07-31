# 🚀 How to Run the QA Automation Platform

The platform runs from whatever folder it lives in — there are no hardcoded
drive letters any more. Replace `<project-folder>` below with your own path
(for example `D:\Ai` or `D:\new ai\agent-ai-`).

---

## ⚡ Option 1: Double-click startup (recommended)

Double-click **`run_server.bat`** in the project folder. It:

1. Switches to its own folder (works on any drive).
2. Checks that `.venv` exists and warns if `.env` is missing.
3. Frees port 8000 if a previous server is still listening.
4. Starts Uvicorn with auto-reload.

---

## 💻 Option 2: Command Prompt (CMD)

```cmd
cd /d <project-folder>
.venv\Scripts\activate.bat
python -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000
```

`qa_agent.py` is a two-line shim over `app/main.py`; `python -m uvicorn app.main:app ...` is equivalent.

## 🟦 Option 3: PowerShell

```powershell
cd '<project-folder>'
.\.venv\Scripts\Activate.ps1
python -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000
```

`qa_agent.py` is a two-line shim over `app/main.py`; `python -m uvicorn app.main:app ...` is equivalent.

## 🐳 Option 4: Docker

```bash
docker compose up --build
```

Test data lives on the `qa-data` volume, mounted at `/data` inside the container.

---

## 🧩 First-time setup

```cmd
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
playwright install chromium
copy .env.example .env
```

Then open `.env` and paste your `GEMINI_API_KEY` (from
<https://aistudio.google.com/apikey>). Recording, execution, verdicts and
reporting all work without a key — only AI test generation needs one.

---

## 🌐 Opening the dashboard

👉 **<http://127.0.0.1:8000>**

The header shows the live workspace path, whether AI is configured, whether the
scheduler is running, and approximate token usage. `GET /health` returns the same
information as JSON if you want to check it from a script.

---

## 🔀 Pointing the platform at a different data folder

```cmd
set QA_WORKSPACE=D:\Ai
python -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000
```

`qa_agent.py` is a two-line shim over `app/main.py`; `python -m uvicorn app.main:app ...` is equivalent.

Everything (`projects/`, `logs/`, `.trash/`, `qa_platform.db`) is created under
that folder.

---

## 🧪 Verifying an install

```cmd
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe tests\e2e_selftest.py
.venv\Scripts\python.exe tests\smoke_ui.py
```

The second command drives a real browser against the bundled sample app in
`tests/selftest_app/` and checks that each scenario reaches the verdict it
should — a fast way to confirm Playwright and Chromium are healthy.
