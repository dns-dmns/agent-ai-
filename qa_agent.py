import sys
import asyncio

# Force Windows to use Proactor Event Loop to support Playwright subprocesses
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import os
import shutil
import time
import json
import google.generativeai as genai
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

app = FastAPI()

# Mount static folder
app.mount("/static", StaticFiles(directory="D:/Ai"), name="static")

# Dynamic migration logic to move legacy folder structure to new Multi-Project layout
def migrate_legacy_flows():
    projects_dir = "D:/Ai/projects"
    legacy_flows_dir = "D:/Ai/flows"
    
    if os.path.exists(legacy_flows_dir) and not os.path.exists(projects_dir):
        default_project_flows = "D:/Ai/projects/ERP_Automation/flows"
        os.makedirs(default_project_flows, exist_ok=True)
        
        for item in os.listdir(legacy_flows_dir):
            src_path = os.path.join(legacy_flows_dir, item)
            dst_path = os.path.join(default_project_flows, item)
            try:
                shutil.move(src_path, dst_path)
            except Exception as e:
                print(f"Error migrating {src_path}: {e}")
                
        try:
            shutil.rmtree(legacy_flows_dir)
        except Exception:
            pass
        print("Successfully migrated legacy flows to projects/ERP_Automation/flows/")

migrate_legacy_flows()

# Global session tracker for active recording browser
active_session = {
    "playwright": None,
    "browser": None,
    "context": None,
    "page": None,
    "recording": False,
    "actions": [],
    "flow_name": None,
    "url": None,
    "project_name": "ERP_Automation",
    "start_time": 0
}

# Unique Selector Generator + Event Recorder JavaScript
JS_RECORDER_SCRIPT = """
(function() {
    if (window.hasPlaywrightRecorder) return;
    window.hasPlaywrightRecorder = true;

    function getUniqueSelector(el) {
        if (el.id) return `#${el.id}`;
        if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;
        if (el.placeholder) return `${el.tagName.toLowerCase()}[placeholder="${el.placeholder}"]`;
        
        if (el.tagName.toLowerCase() === 'button') {
            const text = el.innerText.trim();
            if (text) return `button:has-text("${text}")`;
        }
        
        let path = [];
        let current = el;
        while (current && current.nodeType === Node.ELEMENT_NODE) {
            let selector = current.tagName.toLowerCase();
            if (current.id) {
                selector += '#' + current.id;
                path.unshift(selector);
                break;
            } else {
                let sib = current, nth = 1;
                while (sib = sib.previousElementSibling) {
                    if (sib.tagName === current.tagName) nth++;
                }
                if (nth > 1) selector += `:nth-of-type(${nth})`;
            }
            path.unshift(selector);
            current = current.parentNode;
        }
        return path.join(' > ');
    }

    document.addEventListener('click', (e) => {
        const target = e.target.closest('button, input, select, textarea, a, [role="button"]');
        if (!target) return;
        
        const selector = getUniqueSelector(target);
        const tag = target.tagName.toLowerCase();
        const text = target.innerText ? target.innerText.trim().substring(0, 50) : '';
        
        if (window.recordPlaywrightEvent) {
            window.recordPlaywrightEvent({
                action: 'CLICK',
                selector: selector,
                tag: tag,
                text: text,
                value: ''
            });
        }
    }, true);

    document.addEventListener('change', (e) => {
        const target = e.target;
        if (target.tagName.toLowerCase() !== 'input' && target.tagName.toLowerCase() !== 'textarea' && target.tagName.toLowerCase() !== 'select') return;
        
        const selector = getUniqueSelector(target);
        const tag = target.tagName.toLowerCase();
        
        if (window.recordPlaywrightEvent) {
            window.recordPlaywrightEvent({
                action: 'TYPE',
                selector: selector,
                tag: tag,
                text: '',
                value: target.value
            });
        }
    }, true);
})();
"""

@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    with open("dashboard.html", "r", encoding="utf-8") as file:
        return file.read()

@app.get("/session-status")
async def get_session_status():
    return {"active": os.path.exists("session.json")}

@app.get("/list-projects")
async def list_projects_api():
    projects_dir = "D:/Ai/projects"
    if not os.path.exists(projects_dir):
        os.makedirs(os.path.join(projects_dir, "ERP_Automation", "flows"), exist_ok=True)
        
    projects = [d for d in os.listdir(projects_dir) if os.path.isdir(os.path.join(projects_dir, d))]
    if not projects:
        os.makedirs(os.path.join(projects_dir, "ERP_Automation", "flows"), exist_ok=True)
        projects = ["ERP_Automation"]
    return {"projects": projects}

@app.post("/create-project")
async def create_project_api(project_name: str):
    if not project_name.strip():
        raise HTTPException(status_code=400, detail="Project name cannot be empty.")
    
    project_dir = f"D:/Ai/projects/{project_name.strip()}/flows"
    os.makedirs(project_dir, exist_ok=True)
    return {"status": "success", "message": f"Project '{project_name}' created successfully."}

@app.get("/list-flows")
async def list_flows(project_name: str = "ERP_Automation"):
    flows_dir = f"D:/Ai/projects/{project_name}/flows"
    if not os.path.exists(flows_dir):
        return {"auth_flows": [], "business_flows": []}
    
    # Return separated list of flow subdirectories under selected project
    all_folders = [f for f in os.listdir(flows_dir) if os.path.isdir(os.path.join(flows_dir, f))]
    
    auth_names = ["login", "auth", "session", "logout"]
    auth_flows = []
    business_flows = []
    
    for f in all_folders:
        if any(keyword in f.lower() for keyword in auth_names):
            auth_flows.append(f)
        else:
            business_flows.append(f)
            
    return {"auth_flows": auth_flows, "business_flows": business_flows}

@app.get("/get-flow-details")
async def get_flow_details(flow_name: str, project_name: str = "ERP_Automation"):
    flow_dir = f"D:/Ai/projects/{project_name}/flows/{flow_name}"
    if not os.path.exists(flow_dir):
        raise HTTPException(status_code=404, detail="Flow not found.")
        
    details = {
        "flow": {},
        "actions": [],
        "has_test_cases": os.path.exists(f"{flow_dir}/test_cases.json"),
        "has_scripts": os.path.exists(f"{flow_dir}/scripts.json"),
        "has_test_data": os.path.exists(f"{flow_dir}/test_data.json"),
        "has_documentation": os.path.exists(f"{flow_dir}/documentation.md"),
        "has_suggestions": os.path.exists(f"{flow_dir}/suggestions.json"),
        "has_spec": os.path.exists(f"{flow_dir}/spec.json")
    }
        

    
    try:
        with open(f"{flow_dir}/flow.json", "r", encoding="utf-8") as f:
            details["flow"] = json.load(f)
        with open(f"{flow_dir}/actions.json", "r", encoding="utf-8") as f:
            details["actions"] = json.load(f)
            
        if details["has_test_cases"]:
            with open(f"{flow_dir}/test_cases.json", "r", encoding="utf-8") as f:
                details["test_cases"] = json.load(f)
        if details["has_scripts"]:
            with open(f"{flow_dir}/scripts.json", "r", encoding="utf-8") as f:
                details["scripts"] = json.load(f)
        if details["has_test_data"]:
            with open(f"{flow_dir}/test_data.json", "r", encoding="utf-8") as f:
                details["test_data"] = json.load(f)
        if details["has_documentation"]:
            with open(f"{flow_dir}/documentation.md", "r", encoding="utf-8") as f:
                details["documentation"] = f.read()
        if details["has_suggestions"]:
            with open(f"{flow_dir}/suggestions.json", "r", encoding="utf-8") as f:
                details["suggestions"] = json.load(f)
        if details["has_spec"]:
            with open(f"{flow_dir}/spec.json", "r", encoding="utf-8") as f:
                details["spec"] = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
    return details

@app.post("/create-empty-flow")
async def create_empty_flow_api(flow_name: str, url: str, project_name: str = "ERP_Automation"):
    if not flow_name.strip():
        raise HTTPException(status_code=400, detail="Flow name cannot be empty.")
    if not url.strip():
        raise HTTPException(status_code=400, detail="URL cannot be empty.")
        
    flow_dir = f"D:/Ai/projects/{project_name}/flows/{flow_name.strip()}"
    os.makedirs(f"{flow_dir}/screenshots", exist_ok=True)
    
    flow_metadata = {
        "flow_name": flow_name.strip(),
        "target_url": url.strip(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_seconds": 0,
        "step_count": 0
    }
    
    with open(f"{flow_dir}/flow.json", "w", encoding="utf-8") as f:
        json.dump(flow_metadata, f, indent=4)
        
    with open(f"{flow_dir}/actions.json", "w", encoding="utf-8") as f:
        json.dump([], f, indent=4)
        
    return {"status": "success", "message": f"Empty flow module '{flow_name}' successfully registered under project '{project_name}'."}

@app.post("/create-flow")
async def create_flow_api(flow_name: str, url: str, project_name: str = "ERP_Automation"):
    if not flow_name.strip():
        raise HTTPException(status_code=400, detail="Flow name cannot be empty.")
    if not url.strip():
        raise HTTPException(status_code=400, detail="URL cannot be empty.")
        
    # Check if there's an active session, close it if so
    if active_session["browser"]:
        try:
            await active_session["browser"].close()
            await active_session["playwright"].stop()
        except Exception:
            pass
            
    try:
        # Start headed browser for user interactions
        active_session["playwright"] = await async_playwright().start()
        active_session["browser"] = await active_session["playwright"].chromium.launch(headless=False)
        
        # Load session.json if it exists to preserve logged-in states in project folder
        session_file = f"D:/Ai/projects/{project_name}/session.json"
        if os.path.exists(session_file):
            active_session["context"] = await active_session["browser"].new_context(storage_state=session_file)
        else:
            active_session["context"] = await active_session["browser"].new_context()
            
        active_session["page"] = await active_session["context"].new_page()
        active_session["flow_name"] = flow_name.strip()
        active_session["url"] = url.strip()
        active_session["project_name"] = project_name
        active_session["recording"] = False
        active_session["actions"] = []
        active_session["start_time"] = time.time()
        
        # Open page, waiting for basic layout
        await active_session["page"].goto(url, wait_until="domcontentloaded", timeout=60000)
        
        return {"status": "success", "message": f"Browser opened at {url}. Click 'Start Recording' to begin."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def handle_exposed_event(event):
    if not active_session["recording"]:
        return
        
    active_session["actions"].append(event)
    step_num = len(active_session["actions"])
    
    # Save a temporary screenshot for this step
    temp_path = f"D:/Ai/temp_step_{step_num}.png"
    try:
        page = active_session["page"]
        await page.screenshot(path=temp_path)
        event["screenshot"] = temp_path
    except Exception:
        event["screenshot"] = None

@app.post("/start-recording")
async def start_recording_api():
    if not active_session["page"]:
        raise HTTPException(status_code=400, detail="No active browser session found. Launch browser first!")
        
    try:
        active_session["recording"] = True
        page = active_session["page"]
        context = active_session["context"]
        
        # Expose binding so JavaScript can trigger Python function
        await page.expose_function("recordPlaywrightEvent", lambda ev: asyncio.create_task(handle_exposed_event(ev)))
        
        # Inject recording script on current page
        await page.evaluate(JS_RECORDER_SCRIPT)
        
        # Inject on future pages (redirects / frame loadings)
        await context.add_init_script(JS_RECORDER_SCRIPT)
        
        return {"status": "success", "message": "Recording started. Go to browser and interact with the page."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stop-recording")
async def stop_recording_api():
    if not active_session["page"]:
        raise HTTPException(status_code=400, detail="No active recording found.")
        
    flow_name = active_session["flow_name"]
    url = active_session["url"]
    project_name = active_session["project_name"]
    actions = active_session["actions"]
    duration = int(time.time() - active_session["start_time"])
    
    try:
        # Close browser session
        await active_session["browser"].close()
        await active_session["playwright"].stop()
        
        # Reset session state
        active_session["playwright"] = None
        active_session["browser"] = None
        active_session["context"] = None
        active_session["page"] = None
        active_session["recording"] = False
        
        # Create output folders under the active isolated project namespace
        flow_dir = f"D:/Ai/projects/{project_name}/flows/{flow_name}"
        os.makedirs(f"{flow_dir}/screenshots", exist_ok=True)
        # Clear screenshots folder only to preserve test_cases.json and generated scripts
        shutil.rmtree(f"{flow_dir}/screenshots", ignore_errors=True)
        os.makedirs(f"{flow_dir}/screenshots", exist_ok=True)
        
        # Copy and rename screenshots
        for idx, act in enumerate(actions, start=1):
            temp_img = act.get("screenshot")
            if temp_img and os.path.exists(temp_img):
                target_img = f"{flow_dir}/screenshots/step_{idx}.png"
                shutil.move(temp_img, target_img)
                # Static path mapping relative to mounted static folders
                act["screenshot"] = f"/static/projects/{project_name}/flows/{flow_name}/screenshots/step_{idx}.png"
                
        # Save flow.json metadata
        flow_metadata = {
            "flow_name": flow_name,
            "target_url": url,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": duration,
            "step_count": len(actions)
        }
        
        with open(f"{flow_dir}/flow.json", "w", encoding="utf-8") as f:
            json.dump(flow_metadata, f, indent=4)
            
        # Save actions.json list
        with open(f"{flow_dir}/actions.json", "w", encoding="utf-8") as f:
            json.dump(actions, f, indent=4)
            
        return {"status": "success", "message": f"Recording saved to projects/{project_name}/flows/{flow_name}/"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/analyze-flow")
async def analyze_flow_api(flow_name: str, project_name: str = "ERP_Automation", test_types: str = ""):
    flow_dir = f"D:/Ai/projects/{project_name}/flows/{flow_name}"
    if not os.path.exists(flow_dir):
        raise HTTPException(status_code=404, detail="Flow not found.")
        
    try:
        # Load recorded actions
        with open(f"{flow_dir}/actions.json", "r", encoding="utf-8") as f:
            actions = json.load(f)
            
        with open(f"{flow_dir}/flow.json", "r", encoding="utf-8") as f:
            meta = json.load(f)
            
        # Detect if this is an authentication flow or a business flow
        auth_names = ["login", "auth", "session", "logout"]
        is_auth_flow = any(keyword in flow_name.lower() for keyword in auth_names)
        
        # Build test types guideline if specified
        types_guideline = ""
        if test_types.strip():
            types_guideline = f"The user has selected the following specific test types to cover: {test_types}. You must generate a highly comprehensive set of test cases targeting these categories, ensuring 100% test coverage for each of the selected categories."
        else:
            types_guideline = "Generate a comprehensive, balanced mix of positive, negative, and boundary tests."
        
        if is_auth_flow:
            # Guidelines for generating Authentication test scenarios
            generation_instruction = f"""
            Generate between 10 and 12 comprehensive, logical test cases for an AUTHENTICATION workflow.
            {types_guideline}
            Focus strictly on:
            - Valid credentials (positive login, remember me checks).
            - Invalid credentials (wrong password, empty inputs, formatting validation).
            - Locked users, retry limits, session expiry triggers, and redirection handling.
            Do NOT generate business transactional test cases (like adding, editing customer records, etc).
            """
        else:
            # Guidelines for generating Business transactional test scenarios
            generation_instruction = f"""
            Generate between 12 and 15 comprehensive, logical test cases for a BUSINESS TRANSACTION workflow.
            {types_guideline}
            Focus strictly on:
            - Full CRUD coverage: Ensure scenarios test Creation (e.g., adding a record), View/Read (e.g., searching or verifying details), Editing/Updating a record, and Deletion.
            - Field-by-Field Input Isolation: When generating boundary, negative, or validation test scenarios, isolate tests to ONE field at a time (e.g., test input length boundary on field A while keeping fields B and C valid; test empty input validation on field B while keeping fields A and C valid; test format error on field C while keeping fields A and B valid). Do NOT invalidate multiple fields in a single test case.
            - Module functional logic (e.g. adding, deleting, filtering records, saving lists).
            - Boundary checks, invalid data types, business-rule validation, duplicate validations.
            CRITICAL RULES:
            - DO NOT generate any login or authentication test steps (like typing username/password or hitting the Login button). The platform handles login automatically beforehand.
            - Assume you are already logged in when the test steps begin. Start directly by navigating to the target module page.
            """

        # Call Gemini (Enforces strict zero-auth test generation rules and 22-section QA specification)
        prompt = f"""
        You are a staff QA Test Architect, Principal Quality Engineer, and Enterprise Automation Expert.
        Analyze this recorded flow of user actions on a webpage:
        Flow Name: {flow_name}
        Target URL: {meta['target_url']}
        Recorded Actions:
        {json.dumps(actions, indent=2)}

        Perform a comprehensive QA analysis strictly following the 15-step reasoning process of an Enterprise QA Architect:
        1. Understand business domain.
        2. Understand user's goal.
        3. Understand feature.
        4. Understand business rules.
        5. Understand user roles.
        6. Understand permissions.
        7. Understand dependencies.
        8. Understand integrations.
        9. Understand database impact.
        10. Understand API impact.
        11. Understand security impact.
        12. Understand performance impact.
        13. Understand automation feasibility.
        14. Understand regression impact.
        15. Generate complete QA recommendations.

        Additional Guidelines for this Specific Flow:
        {generation_instruction}

        You must output strictly in JSON format matching this schema.
        CRITICAL: All JSON keys and values must be standard, raw JSON literals. Do NOT use string repeat methods or programmatic multipliers (like '"P".repeat(255)' or '"N" * 100') to represent boundary data. Write out string literals (e.g. 'PPPP...') fully inside the JSON values.
        {{
            "requirement_understanding": "Provide a detailed statement of your business domain understanding and functional objectives.",
            "requirement_gaps": ["List any identified requirements gaps, underspecified rules, or missing workflows."],
            "business_rules": ["List the core business rules and validations observed or required."],
            "assumptions": ["List any operational assumptions, data preconditions, or environment setups."],
            "risks": ["Identify high-risk areas, potential data loss conditions, or critical release risks."],
            "modules_impacted": ["List other parts of the application or database impacted by this change."],
            "test_strategy": {{
                "objectives": "Core testing objectives",
                "scope_in": "What is in scope",
                "scope_out": "What is out of scope",
                "rollback_plan": "Post-release rollback strategy"
            }},
            "test_scenarios": ["List of core test scenarios mapped to CRUD objectives (Create, Read, Update, Delete)"],
            "test_cases": [
                {{
                    "scenario_name": "Detailed scenario name",
                    "type": "positive" | "negative" | "boundary" | "security" | "ui",
                    "steps": [
                        {{
                            "action": "TYPE" | "CLICK",
                            "selector": "CSS selector to use",
                            "value": "Value to input (only if TYPE)"
                        }}
                    ],
                    "expected_result": "Expected validation message, page state, or redirect"
                }}
            ],
            "test_data": {{
                "valid": [{{ "key": "value" }}],
                "invalid": [{{ "key": "value" }}],
                "boundary": [{{ "key": "value" }}]
            }},
            "ui_validation": ["Visual checklist (alignment, contrast, dark-mode, keyboard-navigation tab-order checks)"],
            "api_validation": ["List API contract verifications, schema checks, response code mappings, rate-limiting rules"],
            "database_validation": ["List database checks: state updates, foreign key constraints, primary index locks, history table inserts"],
            "security_validation": ["List OWASP Top 10 security checks: CSRF, SQL Injection, XSS, rate-limits, cookie security"],
            "performance_validation": ["List load metrics, latency targets, caching rules, stress limits"],
            "accessibility_validation": ["List visual contrast, screen reader aria attributes, focus outlines, tab-order mappings"],
            "automation_candidates": ["Identify which parts are highly suitable for CI/CD Playwright test scheduling"],
            "regression_impact": ["List critical regressions, shared libraries, login hooks affected"],
            "defect_prevention_suggestions": ["List specific code fixes, selector improvements, constraint additions to prevent defects"],
            "release_readiness": "Detailed build recommendation (e.g. READY with caveats or NOT READY due to gaps)",
            "coverage_summary": "Requirement mapping summary coverage percentage assessment",
            "ai_self_review": "Exhaustive self-review answering: Did I miss any validation? Did I miss any business rules? Did I miss database checks?",
            "suggestions": {{
                "broken_selectors": ["list of potential selector improvements"],
                "missing_validations": ["list of unhandled edge cases"],
                "recommendations": ["list of general recommendations"]
            }},
            "scripts": {{
                "playwright_python": "Playwright python script code block starting navigation directly to the target page",
                "cypress_js": "Cypress script code block",
                "selenium_java": "Selenium Java code block"
            }},
            "documentation_markdown": "Markdown documentation summarizing the flow step by step"
        }}
        """
        
        model = genai.GenerativeModel(
            model_name='gemini-3.6-flash',
            system_instruction="You are an expert software QA planner. Generate a highly structured, valid JSON test matrix. Output no markdown, only raw JSON."
        )
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        
        raw_text = response.text.strip()
        # Strip markdown wrappers if returned by the LLM
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()
            
        # Pre-repair LLM string multiplier shorthand representations if generated (e.g., "P".repeat(255))
        import re
        raw_text = re.sub(r'"([^"]+)"\.repeat\((\d+)\)', lambda m: '"' + (m.group(1) * int(m.group(2))) + '"', raw_text)
        raw_text = re.sub(r'"([^"]+)"\s*\*\s*(\d+)', lambda m: '"' + (m.group(1) * int(m.group(2))) + '"', raw_text)
        
        try:
            analysis = json.loads(raw_text)
        except Exception as json_err:
            print("=== GEMINI JSON DECODE FAILURE ===")
            print(raw_text)
            print("=================================")
            raise HTTPException(status_code=500, detail=f"Gemini did not return valid JSON: {str(json_err)}")
            
        # Extract keys safely using .get() to prevent KeyErrors
        test_cases = analysis.get("test_cases", [])
        scripts = analysis.get("scripts", {
            "playwright_python": "",
            "cypress_js": "",
            "selenium_java": ""
        })
        test_data = analysis.get("test_data", {
            "valid": [],
            "invalid": [],
            "boundary": []
        })
        doc_markdown = analysis.get("documentation_markdown", "")
        suggestions = analysis.get("suggestions", {
            "broken_selectors": [],
            "missing_validations": [],
            "recommendations": []
        })
        
        # Save assets into separate files in the flow folder
        with open(f"{flow_dir}/test_cases.json", "w", encoding="utf-8") as f:
            json.dump(test_cases, f, indent=4)
            
        with open(f"{flow_dir}/scripts.json", "w", encoding="utf-8") as f:
            json.dump(scripts, f, indent=4)
            
        with open(f"{flow_dir}/test_data.json", "w", encoding="utf-8") as f:
            json.dump(test_data, f, indent=4)
            
        with open(f"{flow_dir}/documentation.md", "w", encoding="utf-8") as f:
            f.write(doc_markdown)
            
        with open(f"{flow_dir}/suggestions.json", "w", encoding="utf-8") as f:
            json.dump(suggestions, f, indent=4)
            
        # Save complete specification spec.json for visual report dashboard
        with open(f"{flow_dir}/spec.json", "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=4)
            
        return {"status": "success", "message": "AI analysis complete. Enterprise QA Specification generated successfully!"}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/run-matrix-stream")
async def run_matrix_stream(flow_name: str, num_cases: int, mode: str, project_name: str = "ERP_Automation", test_types: str = "", auth_flow: str = "", auth_scenario: str = "", dependencies: str = ""):
    session_file = "D:/Ai/session.json"
    flow_dir = f"D:/Ai/projects/{project_name}/flows/{flow_name}"
    login_data_path = "D:/Ai/login_data.json"
    
    if not os.path.exists(f"{flow_dir}/test_cases.json"):
        raise HTTPException(status_code=400, detail="No test cases generated for this flow.")
        
    async def sse_generator():
        yield "data: " + json.dumps({"event": "start", "message": f"Loading matrix test cases for flow '{flow_name}'..."}) + "\n\n"
        
        # Load test cases
        with open(f"{flow_dir}/test_cases.json", "r", encoding="utf-8") as file:
            raw_test_cases = json.load(file)
            
        # Annotate each scenario with its original 1-based index in test_cases.json
        for orig_idx, scenario in enumerate(raw_test_cases, start=1):
            scenario["_original_id"] = orig_idx
            
        # Filter by test type checkboxes (if provided)
        if test_types.strip():
            selected_types = [t.strip().lower() for t in test_types.split(",")]
            filtered_cases = [tc for tc in raw_test_cases if tc.get("type", "").lower() in selected_types]
            if not filtered_cases:
                yield "data: " + json.dumps({"event": "error", "message": f"No test cases found matching types: {test_types}. Running all instead."}) + "\n\n"
                test_cases = raw_test_cases
            else:
                test_cases = filtered_cases
        else:
            test_cases = raw_test_cases
                
        # Limit to num_cases if not -1 (run all)
        if num_cases != -1:
            test_cases = test_cases[:num_cases]
        
        yield "data: " + json.dumps({"event": "info", "message": f"Prepared {len(test_cases)} scenarios to execute in {mode} mode."}) + "\n\n"
        
        headless_mode = (mode == "headless")
        
        # Setup counters for report saving
        passedCount = 0
        failedCount = 0
        allConsoleErrors = []
        allNetworkErrors = []
        run_results = []
        
        # Load login credentials dynamically if they exist
        login_credentials = {}
        if os.path.exists(login_data_path):
            try:
                with open(login_data_path, "r", encoding="utf-8") as lf:
                    login_credentials = json.load(lf)
            except Exception:
                pass
        
        async with async_playwright() as p:
            launch_args = {"headless": headless_mode}
            if not headless_mode:
                launch_args["slow_mo"] = 300
                launch_args["args"] = ["--start-maximized"]
            browser = await p.chromium.launch(**launch_args)
            
            total_cases_run = len(test_cases)
            for run_seq, scenario in enumerate(test_cases, start=1):
                index = scenario.get("_original_id", run_seq)
                name = scenario.get("scenario_name") or scenario.get("scenario") or f"Test Case #{index}"
                expected = scenario.get("expected_result") or scenario.get("expected") or ""
                steps = scenario.get("steps", [])
                
                yield "data: " + json.dumps({
                    "event": "test_start", 
                    "index": index, 
                    "run_seq": run_seq,
                    "total_run": total_cases_run,
                    "name": name,
                    "type": scenario.get("type", "functional")
                }) + "\n\n"
                
                # Load active session cookies (if file exists)
                cookies_active = os.path.exists(session_file)
                context_args = {}
                if cookies_active:
                    context_args["storage_state"] = session_file
                if not headless_mode:
                    context_args["no_viewport"] = True
                context = await browser.new_context(**context_args)
                page = await context.new_page()
                page.set_default_timeout(15000 if not headless_mode else 10000)
                
                # Active events logger lists
                console_errors = []
                network_errors = []
                steps_executed = []
                start_time = time.time()
                
                # Set up listeners
                page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type in ["error", "warning"] else None)
                page.on("requestfailed", lambda req: network_errors.append(f"Network error: {req.method} {req.url} - {req.failure.error_text if req.failure else 'failed'}"))
                
                try:
                    yield "data: " + json.dumps({"event": "log", "message": f"Navigating to target URL: {url}"}) + "\n\n"
                    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(2000)
                    
                    # 🔗 FLOW DEPENDENCY / PREREQUISITE CHAIN RESOLUTION
                    if dependencies.strip():
                        dep_list = [d.strip() for d in dependencies.split(",") if d.strip()]
                        for dep in dep_list:
                            dep_actions_path = f"D:/Ai/projects/{project_name}/flows/{dep}/actions.json"
                            if os.path.exists(dep_actions_path):
                                yield "data: " + json.dumps({"event": "log", "message": f"🔗 Resolving prerequisite dependency: [{dep}]..."}) + "\n\n"
                                with open(dep_actions_path, "r", encoding="utf-8") as f:
                                    dep_actions = json.load(f)
                                
                                for s_num_dep, act_dep in enumerate(dep_actions, start=1):
                                    a_type = act_dep.get("action")
                                    a_sel = act_dep.get("selector")
                                    a_val = act_dep.get("value", "")
                                    
                                    if not a_type or not a_sel:
                                        continue
                                        
                                    yield "data: " + json.dumps({"event": "log", "message": f"🔗 [{dep}] Step {s_num_dep}: {a_type} on '{a_sel}'"}) + "\n\n"
                                    
                                    try:
                                        if a_type == "TYPE":
                                            if not headless_mode:
                                                await page.locator(a_sel).highlight()
                                            await page.fill(a_sel, a_val)
                                        elif a_type == "CLICK":
                                            if not headless_mode:
                                                await page.locator(a_sel).first.highlight()
                                            await page.click(a_sel)
                                    except Exception as dep_step_err:
                                        yield "data: " + json.dumps({"event": "log", "message": f"⚠️ Prerequisite warning on '{a_sel}': {str(dep_step_err)}"}) + "\n\n"
                                        
                                    await page.wait_for_timeout(1000)
                                await page.wait_for_timeout(2000)
                    
                    # 🔐 AUTHENTICATION INTERCEPTION & SMART REUSE (If no explicit dependency chained, check login redirect)
                    is_login_page = "auth/login" in page.url or await page.locator("input[name='username']").count() > 0
                    is_testing_auth_itself = (flow_name == auth_flow) or ("auth/login" in url)
                    
                    if is_login_page and not is_testing_auth_itself and auth_flow and auth_scenario and not dependencies.strip():
                        yield "data: " + json.dumps({
                            "event": "log", 
                            "message": f"🔑 Redirect detected. Executing auth scenario: [{auth_flow} -> {auth_scenario}]"
                        }) + "\n\n"
                        
                        # Load auth flow data from the project's folder structure
                        auth_cases_path = f"D:/Ai/projects/{project_name}/flows/{auth_flow}/test_cases.json"
                        auth_steps = []
                        if os.path.exists(auth_cases_path):
                            with open(auth_cases_path, "r", encoding="utf-8") as f:
                                auth_matrix = json.load(f)
                            auth_case = next((tc for tc in auth_matrix if (tc.get("scenario_name") or tc.get("scenario")) == auth_scenario), None)
                            if auth_case:
                                auth_steps = auth_case.get("steps", [])
                                    
                        # Execute the login steps
                        for s_num_auth, step_auth in enumerate(auth_steps, start=1):
                            act_auth = step_auth.get("action")
                            sel_auth = step_auth.get("selector")
                            val_auth = step_auth.get("value", "")
                            
                            if not act_auth:
                                act_auth = "TYPE" if val_auth else "CLICK"
                                
                            if not sel_auth:
                                continue
                            
                            # Overwrite input fields with central login data dynamically
                            if act_auth == "TYPE":
                                if "username" in sel_auth.lower() or "user" in sel_auth.lower():
                                    val_auth = login_credentials.get("username", val_auth)
                                elif "password" in sel_auth.lower() or "pass" in sel_auth.lower():
                                    val_auth = login_credentials.get("password", val_auth)
                                
                            yield "data: " + json.dumps({"event": "log", "message": f"🔑 Replaying Auth Step {s_num_auth}: {act_auth} on '{sel_auth}'"}) + "\n\n"
                            
                            if act_auth == "TYPE":
                                if not headless_mode:
                                    try:
                                        await page.locator(sel_auth).highlight()
                                    except Exception:
                                        pass
                                await page.fill(sel_auth, val_auth, timeout=10000 if not headless_mode else 5000)
                            elif act_auth == "CLICK":
                                if not headless_mode:
                                    try:
                                        await page.locator(sel_auth).first.highlight()
                                    except Exception:
                                        pass
                                await page.click(sel_auth, timeout=10000 if not headless_mode else 5000)
                                
                            await page.wait_for_timeout(800 if not headless_mode else 500)
                            
                        # Wait for login transition
                        await page.wait_for_timeout(2000)
                        
                        # Save fresh login context
                        await context.storage_state(path=session_file)
                        yield "data: " + json.dumps({"event": "log", "message": "✅ Auth successful! Updated cookies in 'session.json'."}) + "\n\n"
                        
                        # Return to target URL
                        yield "data: " + json.dumps({"event": "log", "message": f"Navigating back to target URL: {url}"}) + "\n\n"
                        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        await page.wait_for_timeout(1000)
                    elif not is_login_page and not is_testing_auth_itself and not dependencies.strip():
                        yield "data: " + json.dumps({"event": "log", "message": "🟢 Active login session detected. Bypassing auth flow."}) + "\n\n"
                        
                    # Parse steps (supports both dict objects and formatted string steps)
                    run_steps = []
                    for st in steps:
                        if isinstance(st, dict):
                            run_steps.append(st)
                        elif isinstance(st, str):
                            raw_str = st.strip()
                            if raw_str.upper().startswith("CLICK ON "):
                                run_steps.append({"action": "CLICK", "selector": raw_str[9:].strip()})
                            elif raw_str.upper().startswith("CLICK "):
                                run_steps.append({"action": "CLICK", "selector": raw_str[6:].strip()})
                            elif raw_str.upper().startswith("TYPE ON ") or raw_str.upper().startswith("TYPE "):
                                prefix_len = 8 if raw_str.upper().startswith("TYPE ON ") else 5
                                rest = raw_str[prefix_len:].strip()
                                if " with value " in rest.lower():
                                    idx_val = rest.lower().find(" with value ")
                                    sel = rest[:idx_val].strip()
                                    val_part = rest[idx_val + 12:].strip()
                                    if (val_part.startswith('"') and val_part.endswith('"')) or (val_part.startswith("'") and val_part.endswith("'")):
                                        val = val_part[1:-1]
                                    else:
                                        val = val_part
                                    run_steps.append({"action": "TYPE", "selector": sel, "value": val})
                                else:
                                    run_steps.append({"action": "TYPE", "selector": rest, "value": ""})
                            else:
                                run_steps.append({"action": "CLICK", "selector": raw_str})

                    for step_num, step in enumerate(run_steps, start=1):
                        action = step.get("action")
                        selector = step.get("selector")
                        value = step.get("value", "")
                        
                        if not action:
                            action = "TYPE" if value else "CLICK"
                            
                        if not selector:
                            continue
                            
                        step_desc = f"{action} on '{selector}'" + (f" with value '{value}'" if action == "TYPE" else "")
                        steps_executed.append(step_desc)
                        
                        yield "data: " + json.dumps({
                            "event": "log", 
                            "message": f"Replaying Step {step_num}: {step_desc}"
                        }) + "\n\n"
                        
                        if action == "TYPE":
                            # Dynamic candidate locators fallback array for input fields
                            type_candidates = [selector]
                            clean_id = selector.lstrip("#").strip()
                            if clean_id:
                                type_candidates.extend([
                                    f'input#{clean_id}',
                                    f'input[name="{clean_id}"]',
                                    f'input[formcontrolname="{clean_id}"]',
                                    f'input[placeholder*="{clean_id}"]',
                                    f'textarea#{clean_id}',
                                    f'textarea[name="{clean_id}"]'
                                ])
                                
                            typed_successfully = False
                            for cand in type_candidates:
                                try:
                                    loc = page.locator(cand).first
                                    if await loc.count() > 0 and await loc.is_visible():
                                        if not headless_mode:
                                            try:
                                                await loc.highlight()
                                            except Exception:
                                                pass
                                        await loc.fill(value, timeout=4000 if not headless_mode else 2500)
                                        typed_successfully = True
                                        break
                                except Exception:
                                    continue
                                    
                            if not typed_successfully:
                                if not headless_mode:
                                    try:
                                        await page.locator(selector).first.highlight()
                                    except Exception:
                                        pass
                                await page.fill(selector, value, timeout=10000 if not headless_mode else 5000)
                        elif action == "CLICK":
                            # Dynamic candidate locators fallback array (handles Cancel, Close, Modal X without re-recording)
                            click_candidates = [selector]
                            sel_lower = selector.lower()
                            
                            if any(kw in sel_lower for kw in ["cancel", "close", "no", "dismiss", "x"]):
                                click_candidates.extend([
                                    'button:has-text("Cancel")',
                                    'button:has-text("Close")',
                                    'button:has-text("No")',
                                    'button:has-text("Dismiss")',
                                    '.p-dialog-footer button:has-text("Cancel")',
                                    'button.p-dialog-header-close',
                                    '.p-dialog-header-close',
                                    '.modal-footer button:has-text("Cancel")',
                                    '.modal-header .btn-close',
                                    'button[aria-label="Close"]',
                                    'button[type="reset"]',
                                    'svg.close-icon',
                                    'i.pi-times'
                                ])
                                
                            clicked_successfully = False
                            
                            for cand in click_candidates:
                                try:
                                    loc = page.locator(cand).first
                                    if await loc.count() > 0 and await loc.is_visible():
                                        if not headless_mode:
                                            try:
                                                await loc.highlight()
                                            except Exception:
                                                pass
                                        await loc.click(timeout=4000 if not headless_mode else 2500)
                                        clicked_successfully = True
                                        break
                                except Exception:
                                    continue
                                    
                            if not clicked_successfully:
                                if not headless_mode:
                                    try:
                                        await page.locator(selector).first.highlight()
                                    except Exception:
                                        pass
                                await page.click(selector, timeout=10000 if not headless_mode else 5000)
                            
                        await page.wait_for_timeout(800 if not headless_mode else 500)
                        
                    # 🔍 LIVE SCREEN INSPECTOR: Auto-discover inline validation messages below input fields
                    try:
                        error_locators = [
                            'small.p-error', 'span.p-error', 'div.p-error',
                            '.invalid-feedback', '.error-message', '.field-error',
                            'p-toast .p-toast-detail', '.toast-message', '.alert-danger',
                            '[role="alert"]'
                        ]
                        detected_messages = []
                        for err_sel in error_locators:
                            err_elements = page.locator(err_sel)
                            cnt = await err_elements.count()
                            for idx_e in range(cnt):
                                el = err_elements.nth(idx_e)
                                if await el.is_visible():
                                    txt = (await el.text_content() or "").strip()
                                    if txt and txt not in detected_messages and len(txt) < 150:
                                        detected_messages.append(txt)
                                        
                        if detected_messages:
                            yield "data: " + json.dumps({
                                "event": "log",
                                "message": f"🔍 Live Screen Inspector: Detected Validation Messages: {detected_messages}"
                            }) + "\n\n"
                    except Exception:
                        pass
                        
                    # Save checkpoint screenshot
                    os.makedirs(f"{flow_dir}/screenshots", exist_ok=True)
                    screenshot_path = f"{flow_dir}/screenshots/result_test_{index}.png"
                    await page.screenshot(path=screenshot_path)
                    
                    elapsed = int(time.time() - start_time)
                    passedCount += 1
                    
                    # Accumulate error logging lists
                    allConsoleErrors.extend(console_errors)
                    allNetworkErrors.extend(network_errors)
                    
                    result_item = {
                        "index": index,
                        "name": name,
                        "status": "PASS",
                        "screenshot": f"/static/projects/{project_name}/flows/{flow_name}/screenshots/result_test_{index}.png",
                        "duration": elapsed,
                        "steps_executed": steps_executed,
                        "expected": expected
                    }
                    run_results.append(result_item)
                    
                    yield "data: " + json.dumps({
                        "event": "test_end", 
                        "index": index, 
                        "status": "PASS", 
                        "screenshot": f"/static/projects/{project_name}/flows/{flow_name}/screenshots/result_test_{index}.png?t={int(time.time())}",
                        "duration": elapsed,
                        "console_errors": console_errors,
                        "network_errors": network_errors,
                        "steps_executed": steps_executed,
                        "expected": expected
                    }) + "\n\n"
                    
                except Exception as test_error:
                    screenshot_path = f"{flow_dir}/screenshots/failed_test_{index}.png"
                    await page.screenshot(path=screenshot_path)
                    elapsed = int(time.time() - start_time)
                    failedCount += 1
                    
                    # Accumulate error logging lists
                    allConsoleErrors.extend(console_errors)
                    allNetworkErrors.extend(network_errors)
                    
                    result_item = {
                        "index": index,
                        "name": name,
                        "status": "FAIL",
                        "screenshot": f"/static/projects/{project_name}/flows/{flow_name}/screenshots/failed_test_{index}.png",
                        "duration": elapsed,
                        "steps_executed": steps_executed,
                        "expected": expected,
                        "error": str(test_error)
                    }
                    run_results.append(result_item)
                    
                    yield "data: " + json.dumps({
                        "event": "test_end", 
                        "index": index, 
                        "status": "FAIL", 
                        "screenshot": f"/static/projects/{project_name}/flows/{flow_name}/screenshots/failed_test_{index}.png?t={int(time.time())}",
                        "error": str(test_error),
                        "duration": elapsed,
                        "console_errors": console_errors,
                        "network_errors": network_errors,
                        "steps_executed": steps_executed,
                        "expected": expected
                    }) + "\n\n"
                finally:
                    await context.close()
                    
            await browser.close()
            
            # Save visual execution reports to JSON files
            report_data = {
                "flow_name": flow_name,
                "project_name": project_name,
                "execution_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_seconds": int(sum(r["duration"] for r in run_results)),
                "test_types_run": test_types,
                "total_test_cases": len(test_cases),
                "passed_count": passedCount,
                "failed_count": failedCount,
                "console_errors": list(set(allConsoleErrors)),
                "network_errors": list(set(allNetworkErrors)),
                "checkpoints": run_results
            }
            
            # Create reports directory and write files
            os.makedirs(f"{flow_dir}/reports", exist_ok=True)
            with open(f"{flow_dir}/report.json", "w", encoding="utf-8") as rf:
                json.dump(report_data, rf, indent=4)
                
            timestamp_report_path = f"{flow_dir}/reports/report_{int(time.time())}.json"
            with open(timestamp_report_path, "w", encoding="utf-8") as rf:
                json.dump(report_data, rf, indent=4)
                
            yield "data: " + json.dumps({"event": "complete", "message": f"Execution finished. Report saved to projects/{project_name}/flows/{flow_name}/report.json"}) + "\n\n"
            
    return StreamingResponse(sse_generator(), media_type="text/event-stream")

@app.post("/upload-test-cases")
async def upload_test_cases(flow_name: str, project_name: str = "ERP_Automation", file: UploadFile = File(...)):
    flow_dir = f"D:/Ai/projects/{project_name}/flows/{flow_name}"
    if not os.path.exists(flow_dir):
        raise HTTPException(status_code=404, detail="Flow not found.")
        
    try:
        contents = await file.read()
        # Validate that it is valid JSON
        test_cases = json.loads(contents.decode("utf-8"))
        
        # Enforce list structure
        if not isinstance(test_cases, list):
            raise HTTPException(status_code=400, detail="JSON must contain a list of test cases.")
            
        # Save to flow's folder
        with open(f"{flow_dir}/test_cases.json", "w", encoding="utf-8") as f:
            json.dump(test_cases, f, indent=4)
            
        return {"status": "success", "message": "Test cases uploaded and saved successfully."}
    except json.JSONDecodeError as je:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file format: {str(je)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# 🤖 MULTI-AGENT TEST CASE GENERATION SYSTEM
# ============================================================================

def _clean_json_response(raw_text: str) -> str:
    """Strip markdown wrappers and repair LLM shorthand from Gemini responses."""
    import re
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw_text = "\n".join(lines).strip()
    raw_text = re.sub(r'"([^"]+)"\.repeat\((\d+)\)', lambda m: '"' + (m.group(1) * int(m.group(2))) + '"', raw_text)
    raw_text = re.sub(r'"([^"]+)"\s*\*\s*(\d+)', lambda m: '"' + (m.group(1) * int(m.group(2))) + '"', raw_text)
    return raw_text


def _build_agent1_prompt(flow_name: str, target_url: str, actions_json: str, fields_context: str, missing_scenarios: list = None, test_types: str = "") -> str:
    """Build Agent 1 (Functional Generator) prompt."""
    missing_instruction = ""
    if missing_scenarios:
        missing_instruction = f"""
        IMPORTANT: A previous review found MISSING scenarios. You MUST generate test cases SPECIFICALLY for these gaps:
        {json.dumps(missing_scenarios, indent=2)}
        Generate ONLY the missing scenarios listed above. Do NOT regenerate scenarios that already exist.
        """
    
    type_filter_instruction = ""
    if test_types.strip():
        type_filter_instruction = f"\n    USER SELECTED TEST TYPE FILTER: Focus generation specifically on these categories: {test_types}\n"
    
    return f"""
    You are Agent 1: The Functional Test Case Generator (The Builder).
    Your ONLY job is to generate POSITIVE, CRUD, WORKFLOW, FIELD VALIDATION, and UI FUNCTIONAL test cases.
    {type_filter_instruction}
    DO NOT generate:
    - Security tests (XSS, SQL injection, CSRF)
    - Boundary value tests (max length, min length)
    - Negative edge case tests (special characters, unicode)
    - Performance or load tests
    
    You MUST generate test cases for these categories:
    - Create with valid data (happy path, all fields, minimum required fields)
    - Dependent Dropdown Selections: Selecting valid active options from prerequisite master dropdowns (e.g. Qualification, Skill, Department, Category)
    - Read/View records (view details modal, field mapping verification)
    - Update/Edit records (modify fields, save changes)
    - Delete records (confirm delete, cancel delete)
    - Search/Filter (exact match, partial match, case-insensitive)
    - Sort columns (ascending/descending for each sortable column)
    - Export functionality (Excel, PDF if available)
    - Dialog interactions (open/close, cancel, form reset)
    - Navigation (menu clicks, page transitions)
    
    Flow Name: {flow_name}
    Target URL: {target_url}
    Recorded Actions:
    {actions_json}
    
    Fields & Context:
    {fields_context}
    
    {missing_instruction}
    
    CRITICAL RULES:
    - DO NOT generate login/authentication steps. Assume already logged in.
    - Each test case MUST start with navigation steps to reach the target module page.
    - Use the EXACT selectors from the recorded actions wherever possible.
    - For dropdowns populated by master tables, use realistic active option names.
    - Generate between 15-25 test cases (or as many as needed to cover all functional scenarios).
    
    Output ONLY a valid JSON array of test case objects in this format:
    [
        {{
            "scenario_name": "Descriptive scenario name",
            "type": "positive" | "positive_ui" | "positive_boundary",
            "steps": [
                {{"action": "CLICK", "selector": "CSS selector"}},
                {{"action": "TYPE", "selector": "CSS selector", "value": "input value"}}
            ],
            "expected_result": "What should happen after execution"
        }}
    ]
    """


def _build_agent2_prompt(flow_name: str, target_url: str, actions_json: str, fields_context: str, missing_scenarios: list = None, test_types: str = "") -> str:
    """Build Agent 2 (Edge Case Generator) prompt."""
    missing_instruction = ""
    if missing_scenarios:
        missing_instruction = f"""
        IMPORTANT: A previous review found MISSING scenarios. You MUST generate test cases SPECIFICALLY for these gaps:
        {json.dumps(missing_scenarios, indent=2)}
        Generate ONLY the missing scenarios listed above. Do NOT regenerate scenarios that already exist.
        """
        
    type_filter_instruction = ""
    if test_types.strip():
        type_filter_instruction = f"\n    USER SELECTED TEST TYPE FILTER: Focus generation specifically on these categories: {test_types}\n"
    
    return f"""
    You are Agent 2: The Edge Case & Security Test Generator (The Breaker).
    Your ONLY job is to generate BOUNDARY, NEGATIVE, SECURITY, INACTIVE EXCLUSION, and ERROR HANDLING test cases.
    {type_filter_instruction}
    
    DO NOT generate:
    - Basic CRUD happy-path scenarios (create with valid data, simple view, simple edit)
    - Normal search or sort tests
    - Standard dialog open/close tests
    
    You MUST generate test cases for these categories:
    - Inactive Master Exclusion: Verify that items marked 'Status = Inactive' (or soft-deleted) in prerequisite master tables (e.g. Inactive Qualification, Inactive Skill, Inactive Department) are EXCLUDED and hidden from dependent selection dropdowns (expected result: item is not listed as a selectable option).
    - Boundary values: Input at max character limit, input exceeding max limit, single character input
    - Negative: Missing required fields, empty submissions, duplicate unique keys
    - Whitespace: Leading/trailing spaces, whitespace-only input, tab characters
    - Special characters: @#$%^&*() in input fields, HTML tags, emoji characters
    - Security: XSS payload (<script>alert('XSS')</script>), SQL injection (' OR 1=1 --), HTML injection
    - Error handling: Network failure graceful handling, timeout behavior, concurrent edits
    - Data validation: Wrong data types (letters in numeric fields), format mismatches (email, phone)
    - Read-only enforcement: Attempt to edit non-editable/disabled fields
    - Overflow: Very long input strings (200+ chars), very large numeric values
    
    Flow Name: {flow_name}
    Target URL: {target_url}
    Recorded Actions:
    {actions_json}
    
    Fields & Context:
    {fields_context}
    
    {missing_instruction}
    
    CRITICAL RULES:
    - DO NOT generate login/authentication steps. Assume already logged in.
    - Each test case MUST start with navigation steps to reach the target module page.
    - Use the EXACT selectors from the recorded actions wherever possible.
    - Generate between 15-25 test cases (or as many as needed to cover all edge cases).
    - For boundary tests, write out full strings literally (e.g., "AAAAAA..." not "A".repeat(50)).
    
    Output ONLY a valid JSON array of test case objects in this format:
    [
        {{
            "scenario_name": "Descriptive scenario name",
            "type": "negative" | "boundary" | "negative_boundary" | "security" | "positive_boundary",
            "steps": [
                {{"action": "CLICK", "selector": "CSS selector"}},
                {{"action": "TYPE", "selector": "CSS selector", "value": "input value"}}
            ],
            "expected_result": "What should happen after execution"
        }}
    ]
    """


def _build_agent3_prompt(merged_cases: list, flow_name: str, fields_context: str) -> str:
    """Build Agent 3 (QA Reviewer) prompt."""
    return f"""
    You are Agent 3: The QA Coverage Reviewer & Auditor.
    Your job is to REVIEW a merged test suite and identify MISSING scenarios.
    
    You must NOT generate test cases yourself. You ONLY analyze and report gaps.
    
    Review the following merged test suite against this COVERAGE MATRIX:
    
    COVERAGE MATRIX (every item must have at least 1 matching test case):
    {{
        "create_happy_path": "At least 1 create with all valid fields",
        "create_minimum_required": "Create with only required fields",
        "dependent_dropdown_selection": "Select valid active master option in relational dropdowns",
        "read_view_record": "View/read a single record details",
        "update_edit_record": "Edit and save changes to a record",
        "delete_confirm": "Delete with confirmation (Yes, Delete)",
        "delete_cancel": "Cancel delete action",
        "search_exact_match": "Search by exact value",
        "search_case_insensitive": "Case-insensitive search",
        "search_no_results": "Search returning no matches",
        "sort_ascending_descending": "Sort by at least 2 columns",
        "required_field_missing": "Submit with missing required field",
        "duplicate_unique_key": "Submit duplicate unique key value",
        "inactive_master_exclusion": "Verify items marked Inactive in master tables are hidden from dependent selection dropdowns",
        "max_length_at_limit": "Input at maximum character limit",
        "max_length_over_limit": "Input exceeding maximum character limit",
        "special_characters_input": "Input with special characters (@#$%)",
        "whitespace_only_input": "Input with only spaces",
        "leading_trailing_spaces": "Input with leading/trailing whitespace",
        "xss_injection": "XSS payload in input field",
        "sql_injection": "SQL injection string in input field",
        "cancel_dialog": "Cancel/close dialog without saving",
        "export_data": "Export/download functionality (Excel/PDF)",
        "empty_optional_fields": "Submit with empty optional fields",
        "readonly_field_check": "Verify read-only fields cannot be edited"
    }}
    
    Flow Name: {flow_name}
    Fields & Context: {fields_context}
    
    MERGED TEST SUITE TO REVIEW:
    {json.dumps(merged_cases, indent=2)}
    
    For each coverage matrix item, check if there is AT LEAST one matching test case in the suite.
    
    Output ONLY a valid JSON object in this format:
    {{
        "coverage_complete": true/false,
        "coverage_percentage": 85,
        "covered_items": ["create_happy_path", "delete_confirm", ...],
        "missing_items": [
            {{
                "matrix_key": "xss_injection",
                "description": "No test case found for XSS payload injection in any input field",
                "suggested_type": "security",
                "priority": "high"
            }}
        ],
        "duplicate_scenarios": ["List of scenario names that appear duplicated"],
        "review_notes": "Overall assessment of the test suite quality"
    }}
    """


@app.get("/generate-multi-agent-stream")
async def generate_multi_agent_stream(flow_name: str, project_name: str = "ERP_Automation", test_types: str = "", max_iterations: int = 3):
    """Multi-Agent Test Case Generation with SSE streaming progress."""
    flow_dir = f"D:/Ai/projects/{project_name}/flows/{flow_name}"
    
    if not os.path.exists(flow_dir):
        raise HTTPException(status_code=404, detail="Flow not found.")
    
    async def sse_generator():
        yield "data: " + json.dumps({"event": "start", "message": "🚀 Multi-Agent Test Generation System initializing..."}) + "\n\n"
        
        # Load flow context
        try:
            with open(f"{flow_dir}/actions.json", "r", encoding="utf-8") as f:
                actions = json.load(f)
            with open(f"{flow_dir}/flow.json", "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as e:
            yield "data: " + json.dumps({"event": "error", "message": f"Failed to load flow data: {str(e)}"}) + "\n\n"
            return
        
        target_url = meta.get("target_url", "")
        actions_json = json.dumps(actions, indent=2)
        
        # Build fields context from recorded actions
        fields_list = []
        for act in actions:
            sel = act.get("selector", "")
            tag = act.get("tag", "")
            text = act.get("text", "")
            action_type = act.get("action", "")
            if sel and (action_type == "TYPE" or tag == "input" or tag == "select" or tag == "textarea"):
                fields_list.append(f"Field: {sel} (tag: {tag}, label: {text})")
            elif sel and action_type == "CLICK" and text:
                fields_list.append(f"Button/Link: {sel} (text: {text})")
        fields_context = "\n".join(fields_list) if fields_list else "No specific fields detected from recording."
        
        # Initialize Gemini model
        model = genai.GenerativeModel(
            model_name='gemini-3.6-flash',
            system_instruction="You are an expert software QA engineer. Output ONLY valid JSON. No markdown, no explanations, no commentary."
        )
        gen_config = {"response_mime_type": "application/json"}
        
        all_cases = []
        iteration = 0
        coverage_complete = False
        
        while iteration < max_iterations and not coverage_complete:
            iteration += 1
            yield "data: " + json.dumps({"event": "round_start", "round": iteration, "max_rounds": max_iterations, "message": f"━━━ Round {iteration}/{max_iterations} ━━━"}) + "\n\n"
            
            missing_for_agent1 = None
            missing_for_agent2 = None
            
            if iteration > 1 and all_cases:
                # For subsequent rounds, we have specific gaps to fill
                functional_gaps = [m for m in last_missing if m.get("suggested_type") in ["positive", "positive_ui", "positive_boundary", "functional"]]
                edge_gaps = [m for m in last_missing if m.get("suggested_type") in ["negative", "boundary", "security", "negative_boundary"]]
                # If gap doesn't have a clear type, send to both
                untyped_gaps = [m for m in last_missing if m.get("suggested_type") not in ["positive", "positive_ui", "positive_boundary", "functional", "negative", "boundary", "security", "negative_boundary"]]
                missing_for_agent1 = functional_gaps + untyped_gaps if functional_gaps or untyped_gaps else None
                missing_for_agent2 = edge_gaps + untyped_gaps if edge_gaps or untyped_gaps else None
            
            # ── Agent 1: Functional Generator ──
            yield "data: " + json.dumps({"event": "agent_start", "agent": 1, "name": "Functional Generator (The Builder)", "message": "🤖 Agent 1 generating functional test cases..."}) + "\n\n"
            
            agent1_cases = []
            try:
                prompt1 = _build_agent1_prompt(flow_name, target_url, actions_json, fields_context, missing_for_agent1, test_types=test_types)
                response1 = model.generate_content(prompt1, generation_config=gen_config)
                raw1 = _clean_json_response(response1.text)
                agent1_cases = json.loads(raw1)
                if not isinstance(agent1_cases, list):
                    agent1_cases = []
                yield "data: " + json.dumps({"event": "agent_done", "agent": 1, "count": len(agent1_cases), "message": f"✅ Agent 1 generated {len(agent1_cases)} functional test cases."}) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"event": "agent_error", "agent": 1, "message": f"⚠️ Agent 1 error: {str(e)}"}) + "\n\n"
            
            # ── Agent 2: Edge Case Generator ──
            yield "data: " + json.dumps({"event": "agent_start", "agent": 2, "name": "Edge Case Generator (The Breaker)", "message": "🤖 Agent 2 generating edge case & security test cases..."}) + "\n\n"
            
            agent2_cases = []
            try:
                prompt2 = _build_agent2_prompt(flow_name, target_url, actions_json, fields_context, missing_for_agent2, test_types=test_types)
                response2 = model.generate_content(prompt2, generation_config=gen_config)
                raw2 = _clean_json_response(response2.text)
                agent2_cases = json.loads(raw2)
                if not isinstance(agent2_cases, list):
                    agent2_cases = []
                yield "data: " + json.dumps({"event": "agent_done", "agent": 2, "count": len(agent2_cases), "message": f"✅ Agent 2 generated {len(agent2_cases)} edge case test cases."}) + "\n\n"
            except Exception as e:
                yield "data: " + json.dumps({"event": "agent_error", "agent": 2, "message": f"⚠️ Agent 2 error: {str(e)}"}) + "\n\n"
            
            # ── Merge & Deduplicate ──
            new_cases = agent1_cases + agent2_cases
            existing_names = {tc.get("scenario_name", "").lower() for tc in all_cases}
            for tc in new_cases:
                name = tc.get("scenario_name", "").lower()
                if name and name not in existing_names:
                    all_cases.append(tc)
                    existing_names.add(name)
            
            yield "data: " + json.dumps({"event": "merge", "total": len(all_cases), "new_added": len(new_cases), "message": f"📋 Merged suite: {len(all_cases)} total unique test cases."}) + "\n\n"
            
            # ── Agent 3: QA Reviewer ──
            yield "data: " + json.dumps({"event": "agent_start", "agent": 3, "name": "QA Reviewer (The Auditor)", "message": "🔍 Agent 3 reviewing coverage and identifying gaps..."}) + "\n\n"
            
            try:
                prompt3 = _build_agent3_prompt(all_cases, flow_name, fields_context)
                response3 = model.generate_content(prompt3, generation_config=gen_config)
                raw3 = _clean_json_response(response3.text)
                review = json.loads(raw3)
                
                coverage_pct = review.get("coverage_percentage", 0)
                covered = review.get("covered_items", [])
                missing = review.get("missing_items", [])
                duplicates = review.get("duplicate_scenarios", [])
                notes = review.get("review_notes", "")
                coverage_complete = review.get("coverage_complete", False)
                last_missing = missing
                
                yield "data: " + json.dumps({
                    "event": "review_done",
                    "agent": 3,
                    "coverage_percentage": coverage_pct,
                    "covered_count": len(covered),
                    "missing_count": len(missing),
                    "coverage_complete": coverage_complete,
                    "missing_items": missing,
                    "review_notes": notes,
                    "message": f"🔍 Coverage: {coverage_pct}% | Covered: {len(covered)} | Gaps: {len(missing)}"
                }) + "\n\n"
                
                if duplicates:
                    yield "data: " + json.dumps({"event": "log", "message": f"🔄 {len(duplicates)} duplicate scenarios detected and excluded."}) + "\n\n"
                
                if coverage_complete:
                    yield "data: " + json.dumps({"event": "log", "message": "🎉 Full coverage achieved! No more iterations needed."}) + "\n\n"
                elif iteration >= max_iterations:
                    yield "data: " + json.dumps({"event": "log", "message": f"⏱️ Max iterations ({max_iterations}) reached. Finalizing with current coverage."}) + "\n\n"
                else:
                    yield "data: " + json.dumps({"event": "log", "message": f"🔁 {len(missing)} gaps found. Starting Round {iteration + 1} to fill them..."}) + "\n\n"
                    
            except Exception as e:
                yield "data: " + json.dumps({"event": "agent_error", "agent": 3, "message": f"⚠️ Agent 3 review error: {str(e)}. Accepting current suite."}) + "\n\n"
                coverage_complete = True
        
        # ── Save final merged suite ──
        if all_cases:
            with open(f"{flow_dir}/test_cases.json", "w", encoding="utf-8") as f:
                json.dump(all_cases, f, indent=4)
            
            yield "data: " + json.dumps({
                "event": "saved",
                "total_cases": len(all_cases),
                "message": f"💾 Final suite saved: {len(all_cases)} test cases → test_cases.json"
            }) + "\n\n"
        
        yield "data: " + json.dumps({
            "event": "complete",
            "total_cases": len(all_cases),
            "rounds_executed": iteration,
            "message": f"✅ Multi-Agent generation complete! {len(all_cases)} test cases generated across {iteration} round(s)."
        }) + "\n\n"
    
    return StreamingResponse(sse_generator(), media_type="text/event-stream")
