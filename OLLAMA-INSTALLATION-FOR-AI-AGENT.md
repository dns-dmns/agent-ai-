# Ollama Installation for AI Agent

How to run this platform's AI test generation on a local model — unlimited
calls, no API key, no quota, and no prompt ever leaves this machine.

Everything in this document was measured on this machine (i5-1235U, 16 GB RAM,
no NVIDIA GPU) during the session of 16 August 2026, when Ollama was installed,
exercised end-to-end, and then removed. Nothing here is guessed.

> **Read §2 before installing anything.** The Ollama support code was removed
> from this codebase along with the installation. Installing Ollama today
> changes nothing until the code in §4 is restored — that is the single most
> important fact in this document.

---

## 1. First decide: is Ollama the right answer?

"Unlimited, free, fast and accurate" — no option gives all four. Pick two-and-a-half:

| Option | Cost | Limit | Speed on this machine | Suite quality | Privacy |
|---|---|---|---|---|---|
| **Gemini free tier** (today) | free | **~20 requests/day/model** — one multi-agent run is up to 9 | fast (seconds) | best | prompts leave the machine |
| **Gemini with billing enabled** | ~a few cents per generation run | effectively unlimited | fast (seconds) | best | prompts leave the machine |
| **Local Ollama, 7B model** | free | **unlimited** | slow (1–10 min per agent) | good, needs a retry sometimes | total — nothing leaves the box |
| **Local Ollama, 3B model** | free | unlimited | **timed out on real prompts — measured, do not use** | — | total |
| **Hosted ollama.com** | account required | free tier is limited too | fast | good | prompts leave the machine |

**Recommendation, in order:**

1. **If a few cents per run is acceptable:** enable billing on the Google
   project and stay on Gemini. Set `QA_LLM_DAILY_BUDGET=0` in `.env` to silence
   the budget warning. This is the accurate + effectively-unlimited option, and
   it needs **no code changes at all**.
2. **If it must be free and unlimited:** local Ollama with
   `qwen2.5-coder:7b`, accepting minutes-per-agent instead of seconds. That is
   what the rest of this document sets up.
3. **Both:** the provider is a config switch once §4 is in place — Gemini when
   quota allows, Ollama when it is spent.

### Why "Ollama is not working" happened before — three real causes, all fixable

| What was observed | Actual cause | Fix |
|---|---|---|
| HTTP 401 from ollama.com | The `gw_live_…` key is an AI-gateway key. It is **not** an ollama.com credential (rejected on every endpoint, with and without auth) and not a Google key either (those start with `AIza`) | Use local Ollama (no key at all), or a genuinely issued key |
| Both agents timed out at 600 s | Model was `llama3.2:3b` — too weak for a ~9,500-token generation prompt on CPU — **and** both agents were sent in parallel to a server that answers one request at a time, so the second spent its whole timeout in a queue | Use the 7B model; run agents sequentially (§4, `main.py`) |
| "Agent generated 0 test cases" while the model had answered correctly | The model returned one scenario as a bare object; the old parser accepted only a list and threw the answer away | Already fixed and committed (`normalise_generated_cases`) — works for any provider |

When it was configured correctly, Ollama **did** work on this machine:
a real generation call returned valid JSON in 24 s cold / 7.4 s warm, and a
Builder-style two-scenario suite in 44 s.

---

## 2. Current state of the codebase — read this first

The Ollama provider layer was **removed** in the reset of 16 August. Today:

- `app/llm.py`, `app/config.py`, `app/main.py` contain **zero** Ollama code.
- Seven places gate generation on `config.GEMINI_API_KEY` directly.
- The two generation agents are dispatched **in parallel** (`asyncio.gather`),
  which is correct for Gemini and wrong for a local Ollama.

So the order of work is: install Ollama (§3) → restore the provider code (§4)
→ configure `.env` (§5) → run the test checklist (§6). Skipping §4 gives you a
running Ollama that the platform never calls.

---

## 3. Installing Ollama

```powershell
winget install Ollama.Ollama
ollama pull qwen2.5-coder:7b        # ~4.4 GB download
```

Facts to know before you start (verified on this machine):

- **Disk:** ~7.1 GB total — 2.74 GB program (mostly GPU runtimes), 4.36 GB model
  in `%USERPROFILE%\.ollama` (the uninstaller does NOT delete that folder).
- **Autostart:** the installer drops a shortcut in the Startup folder, so Ollama
  runs after every reboot as a background tray app on `127.0.0.1:11434`. There
  is no Windows service — do not look for one.
- **No key, no account** is needed for local use.
- **Model choice:** `qwen2.5-coder:7b` is the smallest model that completed the
  platform's real generation prompts here. `llama3.2:3b` did not — measured,
  not assumed. Anything bigger than 7–8B will be painful on 16 GB RAM without a GPU.
- **Vision:** `llama3.2-vision:11b` (~8 GB more) is only needed if
  `QA_VISION_HEALING=1`. Leave healing off and skip the download.

Verify the install:

```powershell
curl http://127.0.0.1:11434/api/tags       # must list qwen2.5-coder:7b
```

---

## 4. Files to update in this application

These changes existed and worked on 16 August; they were reverted with the
uninstall. This section is the rebuild map. (Claude can restore all of it on
request — say "rebuild the Ollama provider layer".)

| File | Status | What changes |
|---|---|---|
| `app/ollama_client.py` | **NEW** | The transport. Plain `urllib` POST to `http://127.0.0.1:11434/api/chat` with `stream:false`, `format:"json"`, `options.num_ctx`. No SDK — a supply chain for a localhost HTTP call is pointless. Helpers: `is_running()`, `has_model()`, `list_models()` via `/api/tags`. Refuse non-loopback hosts unless `OLLAMA_ALLOW_REMOTE=1` (prompts carry recorded selectors and values — sending them somewhere must be deliberate). |
| `app/config.py` | edit | Add `LLM_PROVIDER` (`QA_LLM_PROVIDER`, default `gemini`), `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`), `OLLAMA_MODEL` (default `qwen2.5-coder:7b`), `OLLAMA_NUM_CTX` (default `16384`), `OLLAMA_TIMEOUT` (default `600`), `OLLAMA_TEMPERATURE` (`0.2`), `OLLAMA_ALLOW_REMOTE` (`0`). |
| `app/llm.py` | edit | Provider dispatch in `generate_text`: when provider is `ollama`, call the client instead of the Gemini SDK. `require_key()` becomes provider-aware ("is the server up, is the model pulled" — each with its one-line fix). Add `is_ready()` / `not_ready_message()` so callers never invent their own gate. Error messages must name the provider that actually ran and the real attempt count — "Gemini request failed after 4 attempts" for an Ollama 401 that was never retried sent debugging in the wrong direction. Make the Gemini SDK import guard conditional on the provider, so a machine without `google-genai` can still start. |
| `app/main.py` | edit | **(a)** Replace all seven `config.GEMINI_API_KEY` gates with `llm.is_ready()` / `llm.not_ready_message()` — lines to find: the generation-stream gate, the selector-repair gate, the plain-English-rules gate, two `ai_configured` status fields, `/llm-status`, and the startup log line. **(b)** In the multi-agent stream: when the provider is Ollama, run the two agents **sequentially**. A local server answers one request at a time; gathering them in parallel leaves agent 2 queued while its own timeout clock runs — that is precisely the double-timeout that was observed. |
| `app/heuristics.py` | edit | Vision-healing gate: `llm.is_ready()` instead of the Gemini key check (line ~107). |
| `app/verdict.py` | edit | AI-adjudication gate: same substitution (line ~559). |
| `.env` | edit | See §5. |
| `tests/` | add | `verify_ollama.py` (round-trip proof, §6 step 2) and unit tests: provider dispatch, sequential-agent rule, "a 401 names the model when the model is the problem", "no module gates on the Gemini key directly". |

**Nothing else changes.** Recording, execution, verdicts, reports, coverage,
defects, export — none of it calls a model.

---

## 5. `.env` settings — and the three silent killers

```ini
QA_LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
OLLAMA_MODEL=qwen2.5-coder:7b
OLLAMA_NUM_CTX=16384
OLLAMA_TIMEOUT=600
QA_VISION_HEALING=0
```

The three settings that fail **silently** if wrong:

1. **`OLLAMA_NUM_CTX=16384`** — Ollama's default context is 2048 tokens and it
   **truncates longer prompts without any error**. The platform's generation
   prompts reach ~9,500 tokens. At the default, the model answers from half the
   recording and the output *parses fine* — a plausible-looking suite that is
   missing half your flow. This is the worst of the three because nothing ever
   looks broken.
2. **`OLLAMA_TIMEOUT=600`** — a 7B model on this CPU takes minutes, not
   seconds, for a full generation prompt. The default HTTP timeouts elsewhere
   in the stack are far too short.
3. **`QA_VISION_HEALING=0`** — unless you also pulled the 11 GB vision model,
   healing sends screenshots to a text model that cannot read them: 15–20 s
   wasted per failed selector, learning nothing.

---

## 6. Test checklist before handing to the testing team

Run these in order. Each has a pass condition; stop at the first failure.

**Step 1 — the platform still works with no model at all.**
```powershell
.venv\Scripts\python.exe -m pytest tests -q          # 145 tests, all pass
.venv\Scripts\python.exe tests\e2e_selftest.py       # SELF-TEST PASSED
.venv\Scripts\python.exe tests\e2e_chain.py          # CHAIN SELF-TEST PASSED
.venv\Scripts\python.exe tests\smoke_ui.py           # UI SMOKE TEST PASSED
.venv\Scripts\python.exe tests\integration_api.py    # API INTEGRATION PASSED
.venv\Scripts\python.exe tests\regression_existing_data.py
```
Pass: all six green. These need no key and no Ollama — if they fail, the
problem is not the provider.

**Step 2 — the model round-trips.** (`tests/verify_ollama.py` from §4.)
Checks, in order: server answers on 11434 → configured model is pulled →
host is loopback (privacy) → a real `/api/chat` call returns parseable JSON →
a Builder-style prompt yields a scenario list with steps. Pass: all checks
green; note the timings it prints — that is your baseline.

**Step 3 — warm the model, then generate a real suite.**
The first call after idle loads 4.4 GB into RAM (~18–25 s); a `keep_alive`
call pins it for 30 minutes. Then, in the dashboard: open a project with a
real recording (Ambica → Login), run **multi-agent generation, rounds = 1**.

Pass criteria — check every one:
- No agent error lines; each agent line shows a real count (`✅ … generated N`).
  A `❌ … 0 test cases` line means the response held nothing — retry once (a
  small model occasionally needs it), then check `logs/qa_platform.log`.
- Open the generated `test_cases.json`:
  - **Selectors are grounded** — every selector appears in `actions.json`, or
    the response carries a grounding warning. A suite inventing selectors is
    the Beamer-Capacity failure; do not hand it over.
  - **Negative cases carry wrong values** (`wrong1`, blanks, injection
    strings), not the real credentials.
  - Credentials appear only as `{{username}}` / `{{password}}`, never literals.
- Run the generated suite once (headless) and read the report: verdicts must be
  reasoned (evidence-based PASS/FAIL/UNVERIFIED), not all-UNVERIFIED — that
  would mean the expected results are unverifiable wording.

**Step 4 — the failure messages are honest.** Stop Ollama
(`Get-Process ollama | Stop-Process -Force`), click generate: the message must
say the server is not answering and how to start it — not "check your API
key". Start it again, set `OLLAMA_MODEL=does-not-exist`, generate: the message
must name the model and list what is actually pulled. Restore the model name.

**Step 5 — record the timings for the team.** Fill this table into the
handover note; measured values from 16 August as reference:

| Operation | Cold (model not in RAM) | Warm |
|---|---|---|
| Trivial JSON call | 24 s | 7 s |
| One generation agent, real prompt | — | 33 s – 10 min (varies with recording size) |
| Full multi-agent round (sequential) | — | roughly the sum of both agents + auditor |

---

## 7. Why agents are sometimes slow, and what actually helps

The variance ("sometimes it takes more time") has exactly four causes here:

1. **Cold start** — first call after ~5 idle minutes reloads 4.4 GB into RAM.
   *Fix:* a warm-up call with `keep_alive: "30m"` when the server starts, and
   before a demo.
2. **One request at a time** — a local Ollama serializes requests. Parallel
   agents don't fail loudly; the second one just waits, then times out.
   *Fix:* the sequential dispatch in §4 (`main.py`). Do not raise worker counts.
3. **Prompt size scales with the recording** — a 35-step recording with effect
   blocks is a much longer prompt than a 6-step login, and CPU inference is
   roughly linear in it. *Fix:* accept it, or generate per-module rather than
   giant flows. Do **not** shrink `QA_PROMPT_ACTIONS_LIMIT` to speed things up —
   starving the model of the recording is how fabricated suites happen.
4. **Other load** — TestPilot, browsers and the dev server share the same
   10 CPU cores. *Fix:* generate when a suite run isn't also executing.

What does **not** help: retrying faster (the queue is the queue), a bigger
`num_ctx` than needed (slower for nothing), or a 3B model (fails outright).

---

## 8. Troubleshooting — every symptom seen on this machine

| Symptom | Cause | Fix |
|---|---|---|
| `HTTP 401` from ollama.com | `gw_live_…` is not an ollama.com key | Local Ollama needs no key; delete `OLLAMA_API_KEY` |
| `timed out after 600s`, both agents | 3B model, or parallel dispatch to a local server | 7B model + sequential agents (§4) |
| `Agent generated 0 test cases` but no error | Model answered in an unexpected shape | Already handled (`normalise_generated_cases`); retry once, then read `logs/qa_platform.log` |
| Suite parses but is missing half the flow | `num_ctx` at default 2048 — silent truncation | `OLLAMA_NUM_CTX=16384` (§5) |
| First call after a break is very slow | Model reloading into RAM | Warm-up + `keep_alive` (§7) |
| `GEMINI_API_KEY is not configured` while using Ollama | A gate still checks the Gemini key — §4 `main.py` changes not applied | Apply §4; every gate must go through `llm.is_ready()` |
| Nothing on port 11434 | Ollama not running (autostart shortcut removed?) | Start "Ollama" from the Start menu, or `ollama serve` |
| Port confusion | 8080 is TestPilot, 8000 is this platform, 11434 is Ollama | Three different applications — check the port before diagnosing |

---

## 9. Uninstalling cleanly (for completeness)

```powershell
Get-Process ollama, "ollama app" -ErrorAction SilentlyContinue | Stop-Process -Force
winget uninstall --id Ollama.Ollama
Remove-Item "$env:USERPROFILE\.ollama" -Recurse -Force      # the 4.4 GB the uninstaller skips
Remove-Item "$env:LOCALAPPDATA\Ollama" -Recurse -Force
```

Then check the **user PATH**: the uninstaller leaves its entry behind
(verified on this machine) — remove
`C:\Users\<you>\AppData\Local\Programs\Ollama` from
Settings → Environment Variables. Finally set `QA_LLM_PROVIDER=gemini` in
`.env`, or generation stays blocked with an "Ollama is not answering" message.
