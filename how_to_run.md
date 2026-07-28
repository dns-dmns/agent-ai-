# 🚀 How to Run the QA Automation Platform

Here are the step-by-step instructions to activate your Python environment and launch the FastAPI server.

---

## ⚡ Option 1: Double-Click Startup (CMD Batch)
Double-click the **`start_server.bat`** file located in the root `D:\Ai` folder. This automatically:
1. Navigates to `D:\Ai`.
2. Activates the `.venv` virtual environment correctly.
3. Launches the Uvicorn server on port 8000.

---

## 💻 Option 2: Command Prompt (CMD) Manual Startup
Open your standard Command Prompt (CMD) window, navigate to the folder, and run:
```cmd
cd /d D:\Ai
.venv\Scripts\activate.bat
python -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000
```
*(Notice the backslashes `\` instead of forward slashes `/`)*

---

## 🟦 Option 3: PowerShell Manual Startup
Open a PowerShell window, navigate to the folder, and run:
```powershell
cd D:\Ai
.\.venv\Scripts\Activate.ps1
python -m uvicorn qa_agent:app --host 127.0.0.1 --port 8000
```

---

## 🌐 Opening the Dashboard
Once the server started, open your web browser and navigate to:
👉 **[http://127.0.0.1:8000](http://127.0.0.1:8000)**
