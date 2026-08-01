/* Application logic for the AI QA Automation Platform dashboard.
 * Extracted verbatim from the original single-file dashboard so the UI can be
 * redesigned without touching behaviour. Every function name and element id the
 * markup relies on is unchanged.
 */

let currentProjectName = "";
        let currentFlowName = "";
        let flowMetadata = {};
        let flowSuggestions = {};

        let globalAuthFlows = [];
        let globalBusinessFlows = [];

        // Toggle custom multi-select dropdown menu
        function toggleDepDropdown(event) {
            if (event) event.stopPropagation();
            const container = document.getElementById('depMultiselectContainer');
            if (container) {
                container.classList.toggle('open');
            }
        }

        // Close dropdown when clicking anywhere outside
        document.addEventListener('click', function (e) {
            const container = document.getElementById('depMultiselectContainer');
            if (container && !container.contains(e.target)) {
                container.classList.remove('open');
            }
        });

        // Toggle manual dependency input box
        function toggleDepManualInput() {
            const box = document.getElementById('depManualBox');
            const cb = document.getElementById('depOtherCheckbox');
            if (box && cb) {
                box.style.display = cb.checked ? 'block' : 'none';
                if (cb.checked) {
                    const manualInput = document.getElementById('profileDependenciesManual');
                    if (manualInput) manualInput.focus();
                }
            }
            updateDependenciesHiddenInput();
        }

        // Calculate and update the hidden #profileDependencies input value and selected pills display
        function updateDependenciesHiddenInput() {
            const selected = [];
            const checkboxes = document.querySelectorAll('input[name="depFlowItem"]:checked');
            checkboxes.forEach(cb => selected.push(cb.value));

            const pillsArea = document.getElementById('depSelectedPills');
            if (pillsArea) {
                pillsArea.innerHTML = '';
                if (selected.length === 0) {
                    pillsArea.innerHTML = '<span class="placeholder-text">Select flow prerequisites...</span>';
                } else {
                    selected.forEach(item => {
                        const isAuth = ["login", "auth", "session", "logout"].some(kw => item.toLowerCase().includes(kw));
                        const pill = document.createElement('span');
                        pill.className = 'dep-pill';
                        pill.innerHTML = `<span>${isAuth ? '🔑' : '💼'} ${item}</span>`;
                        pillsArea.appendChild(pill);
                    });
                }
            }

            const otherCb = document.getElementById('depOtherCheckbox');
            if (otherCb && otherCb.checked) {
                const manualInput = document.getElementById('profileDependenciesManual');
                if (manualInput && manualInput.value.trim()) {
                    const customItems = manualInput.value.split(',').map(s => s.trim()).filter(Boolean);
                    customItems.forEach(item => {
                        if (!selected.includes(item)) selected.push(item);
                    });
                }
            }

            const hiddenInput = document.getElementById('profileDependencies');
            if (hiddenInput) {
                hiddenInput.value = selected.join(',');
            }
        }

        // Render dependency multi-select options inside custom dropdown
        function renderDependencyOptions() {
            const optionsContainer = document.getElementById('depDropdownOptions');
            if (!optionsContainer) return;

            optionsContainer.innerHTML = '';
            const allFlows = [...globalAuthFlows, ...globalBusinessFlows];
            const currentLower = (currentFlowName || "").toLowerCase();
            const validFlows = allFlows.filter(f => f.toLowerCase() !== currentLower);

            const isAuthFlowTarget = ["login", "auth", "session", "logout"].some(kw => currentLower.includes(kw));

            if (validFlows.length === 0) {
                optionsContainer.innerHTML = '<div style="font-size:0.78rem; color: var(--text-muted); padding: 0.4rem;">No other flows available in project</div>';
            } else {
                validFlows.forEach(flow => {
                    const isAuth = ["login", "auth", "session", "logout"].some(kw => flow.toLowerCase().includes(kw));
                    // Check 'login' or auth flows by default if target is a business flow
                    const isChecked = (!isAuthFlowTarget && isAuth) ? 'checked' : '';

                    const label = document.createElement('label');
                    label.className = 'multiselect-option';
                    label.innerHTML = `
                        <input type="checkbox" name="depFlowItem" value="${flow}" ${isChecked} onchange="updateDependenciesHiddenInput()">
                        <span>${isAuth ? '🔑' : '💼'} ${flow}</span>
                    `;
                    optionsContainer.appendChild(label);
                });
            }

            // Always append 'Other / Custom' option
            const otherLabel = document.createElement('label');
            otherLabel.className = 'multiselect-option multiselect-option-other';
            otherLabel.innerHTML = `
                <input type="checkbox" id="depOtherCheckbox" onchange="toggleDepManualInput()">
                <span>✏️ Other / Custom</span>
            `;
            optionsContainer.appendChild(otherLabel);

            // Update hidden input and trigger pills display after rendering
            updateDependenciesHiddenInput();
        }

        // Toggle Views Javascript Controller
        function switchMainTab(viewId, btn) {
            document.querySelectorAll('.main-view').forEach(el => el.classList.remove('active'));
            document.getElementById(viewId).classList.add('active');

            document.querySelectorAll('.main-tab-btn').forEach(el => el.classList.remove('active'));
            btn.classList.add('active');
        }

        // Toggles between subtabs inside the Principal QA Architect Specification Cockpit
        function switchSpecTab(tabId, btn) {
            document.querySelectorAll('.spec-tab-content').forEach(el => el.style.display = 'none');
            document.getElementById(tabId).style.display = 'flex';

            btn.closest('div').querySelectorAll('.btn-tab').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
        }

        // Load statistics and checklist items for Tab 3 Project Console
        async function loadProjectConsoleStats() {
            document.getElementById('statsProjectName').innerText = currentProjectName;

            try {
                const response = await fetch(`/list-flows?project_name=${encodeURIComponent(currentProjectName)}`, { cache: "no-store" });
                const data = await response.json();

                const flows = [...data.auth_flows, ...data.business_flows];

                let totalModules = flows.length;
                let analyzedModules = 0;
                let pendingModules = 0;
                let totalTestCases = 0;

                const tableBody = document.getElementById('statsModulesTableBody');
                tableBody.innerHTML = '';

                if (totalModules === 0) {
                    tableBody.innerHTML = `<tr><td colspan="5" style="padding: 1.5rem; text-align:center; color: var(--text-muted);">No modules registered in this project. Create one using the panel above!</td></tr>`;
                    document.getElementById('statsTotalModules').innerText = 0;
                    document.getElementById('statsAnalyzedModules').innerText = 0;
                    document.getElementById('statsPendingModules').innerText = 0;
                    document.getElementById('statsTotalTestCases').innerText = 0;
                    return;
                }

                // Fetch details for all modules in parallel
                const detailsPromises = flows.map(f => fetch(`/get-flow-details?flow_name=${encodeURIComponent(f)}&project_name=${encodeURIComponent(currentProjectName)}`, { cache: "no-store" }).then(r => r.json()));
                const detailsList = await Promise.all(detailsPromises);

                detailsList.forEach((details, idx) => {
                    const name = flows[idx];
                    const isAuth = ["login", "auth", "session", "logout"].some(kw => name.toLowerCase().includes(kw));
                    const typeLabel = isAuth ? '🔑 Authentication' : '💼 Business Flow';

                    const hasCases = details.has_test_cases;
                    let statusLabel = '';
                    let actionPending = '';
                    let casesCount = 0;

                    if (hasCases) {
                        analyzedModules++;
                        casesCount = details.test_cases ? details.test_cases.length : 10;
                        totalTestCases += casesCount;
                        statusLabel = `<span style="color: var(--accent-green); font-weight:600;">🟢 Analyzed</span>`;
                        actionPending = `<span style="color: var(--text-muted);">None (Ready to Run)</span>`;
                    } else {
                        pendingModules++;
                        statusLabel = `<span style="color: var(--accent-yellow); font-weight:600;">🟡 Unanalyzed</span>`;
                        actionPending = `<span style="color: #3b82f6; font-weight: 600; cursor:pointer;" onclick="triggerConsoleAIAnalysis('${name}', this)">⚡ Run AI Analysis</span>`;
                    }

                    const row = document.createElement('tr');
                    row.style.borderBottom = '1px solid var(--border-color)';
                    row.innerHTML = `
                        <td style="padding: 0.75rem 1rem; font-weight:600;">${name}</td>
                        <td style="padding: 0.75rem 1rem; color: var(--text-muted);">${typeLabel}</td>
                        <td style="padding: 0.75rem 1rem;">${statusLabel}</td>
                        <td style="padding: 0.75rem 1rem; color: white;">${casesCount} Scenarios</td>
                        <td style="padding: 0.75rem 1rem;">${actionPending}</td>
                    `;
                    tableBody.appendChild(row);
                });

                document.getElementById('statsTotalModules').innerText = totalModules;
                document.getElementById('statsAnalyzedModules').innerText = analyzedModules;
                document.getElementById('statsPendingModules').innerText = pendingModules;
                document.getElementById('statsTotalTestCases').innerText = totalTestCases;

                // Fetch and render bugs list in Project Console
                try {
                    const bugsResponse = await fetch(`/get-project-bugs?project_name=${encodeURIComponent(currentProjectName)}`, { cache: "no-store" });
                    if (bugsResponse.ok) {
                        const bugsData = await bugsResponse.json();
                        const bugsList = bugsData.bugs || [];
                        
                        const badge = document.getElementById('bugsCountBadge');
                        const listContainer = document.getElementById('projectBugsList');
                        
                        badge.innerText = `${bugsList.length} Bugs`;
                        
                        if (bugsList.length === 0) {
                            listContainer.innerHTML = `<div style="color: var(--text-muted); font-family: sans-serif; text-align: center; padding: 1.5rem;">No failing test cases found in this project. Everything is green!</div>`;
                        } else {
                            listContainer.innerHTML = '';
                            bugsList.forEach((bug, idx) => {
                                const line = document.createElement('div');
                                line.style.borderBottom = '1px solid rgba(239, 68, 68, 0.15)';
                                line.style.padding = '0.4rem 0.2rem';
                                line.style.wordBreak = 'break-all';
                                line.innerText = `${idx + 1}. ${bug.description}`;
                                listContainer.appendChild(line);
                            });
                        }
                    }
                } catch (bugErr) {
                    console.error("Error loading bugs:", bugErr);
                }

            } catch (e) {
                console.error(e);
            }
        }

        // --- Project Test Cases Explorer & Playback Hub Javascript ---
        async function loadExplorerFlowsList() {
            const projectSelect = document.getElementById('explorerProjectSelect');
            const flowsContainer = document.getElementById('explorerFlowsListContainer');
            const selectedFlowLabel = document.getElementById('explorerSelectedFlowName');
            const casesContainer = document.getElementById('explorerTestCasesListContainer');

            selectedFlowLabel.innerText = "None selected";
            casesContainer.innerHTML = `<div style="color: var(--text-muted); font-size: 0.82rem; text-align: center; margin-top: 2rem;">Click a flow module from the left list to explore its test cases and watch execution videos.</div>`;
            
            if (!projectSelect) return;
            const projectName = projectSelect.value;
            if (!projectName) {
                flowsContainer.innerHTML = `<div style="color: var(--text-muted); font-size: 0.8rem;">Select a project first.</div>`;
                return;
            }

            flowsContainer.innerHTML = `<div style="color: var(--text-muted); font-size: 0.8rem;">Loading flows...</div>`;

            try {
                const response = await fetch(`/list-flows?project_name=${encodeURIComponent(projectName)}`, { cache: "no-store" });
                const data = await response.json();
                const flows = [...data.auth_flows, ...data.business_flows];

                flowsContainer.innerHTML = '';
                if (flows.length === 0) {
                    flowsContainer.innerHTML = `<div style="color: var(--text-muted); font-size: 0.8rem;">No flows registered in this project.</div>`;
                    return;
                }

                flows.forEach(flow => {
                    const btn = document.createElement('button');
                    btn.className = 'btn btn-outline explorer-flow-btn';
                    btn.style.width = '100%';
                    btn.style.textAlign = 'left';
                    btn.style.padding = '0.5rem 0.75rem';
                    btn.style.fontSize = '0.8rem';
                    btn.style.display = 'block';
                    btn.style.marginBottom = '4px';
                    btn.innerText = flow;
                    btn.onclick = () => {
                        // Highlight active button
                        document.querySelectorAll('.explorer-flow-btn').forEach(b => {
                            b.style.borderColor = 'rgba(255, 255, 255, 0.15)';
                            b.style.background = 'transparent';
                        });
                        btn.style.borderColor = '#a78bfa';
                        btn.style.background = 'rgba(167, 139, 250, 0.05)';
                        loadExplorerTestCases(flow);
                    };
                    flowsContainer.appendChild(btn);
                });
            } catch (e) {
                console.error(e);
                flowsContainer.innerHTML = `<div style="color: var(--accent-red); font-size: 0.8rem;">Failed to load flows.</div>`;
            }
        }

        async function loadExplorerTestCases(flowName) {
            const projectSelect = document.getElementById('explorerProjectSelect');
            const projectName = projectSelect.value;
            const casesContainer = document.getElementById('explorerTestCasesListContainer');
            const selectedFlowLabel = document.getElementById('explorerSelectedFlowName');

            selectedFlowLabel.innerText = flowName;
            casesContainer.innerHTML = `<div style="color: var(--text-muted); font-size: 0.82rem; text-align: center; margin-top: 2rem;">Loading test scenarios...</div>`;

            try {
                const response = await fetch(`/get-flow-details?flow_name=${encodeURIComponent(flowName)}&project_name=${encodeURIComponent(projectName)}`, { cache: "no-store" });
                const details = await response.json();
                
                const testCases = details.test_cases || [];

                casesContainer.innerHTML = '';
                if (testCases.length === 0) {
                    casesContainer.innerHTML = `<div style="color: var(--text-muted); font-size: 0.82rem; text-align: center; margin-top: 2rem;">No test scenarios generated yet for this flow.</div>`;
                    return;
                }

                testCases.forEach((tc, tcIdx) => {
                    let statusHtml = '<span style="color: var(--text-muted); font-size:0.75rem;">⚪ Not Executed</span>';
                    let videoButtonHtml = '';
                    
                    if (details.latest_report && details.latest_report.checkpoints) {
                        const matchedCheckpoint = details.latest_report.checkpoints.find(cp => 
                            cp.name === tc.scenario_name || cp.name === tc.scenario || cp.index === (tcIdx + 1)
                        );
                        if (matchedCheckpoint) {
                            const status = matchedCheckpoint.status;
                            const statusColor = status === 'PASS' ? 'var(--accent-green)' : 'var(--accent-red)';
                            statusHtml = `<span style="color: ${statusColor}; font-weight:600; font-size:0.75rem;">${status === 'PASS' ? '🟢 PASS' : '🔴 FAIL'}</span>`;
                            if (matchedCheckpoint.video) {
                                videoButtonHtml = `
                                    <button class="btn" style="background: #10b981; color: white; padding: 2px 8px; font-size: 0.72rem; display: flex; align-items: center; gap: 4px; border-radius: 4px;" onclick="openVideoModal('${matchedCheckpoint.video}')">
                                        🎥 Play Run
                                    </button>
                                `;
                            }
                        }
                    }

                    const item = document.createElement('div');
                    item.style.padding = '0.5rem 0.25rem';
                    item.style.borderBottom = '1px solid var(--border-color)';
                    item.style.display = 'flex';
                    item.style.justifyContent = 'space-between';
                    item.style.alignItems = 'center';
                    item.innerHTML = `
                        <div style="max-width: 70%;">
                            <div style="font-weight: 600; font-size:0.82rem; color:white;">${tcIdx + 1}. ${tc.scenario_name || tc.scenario}</div>
                            <div style="font-size: 0.72rem; color: var(--text-muted); margin-top: 2px;">Type: ${tc.type || 'functional'}</div>
                        </div>
                        <div style="display: flex; align-items: center; gap: 0.6rem;">
                            ${statusHtml}
                            ${videoButtonHtml}
                        </div>
                    `;
                    casesContainer.appendChild(item);
                });
            } catch (e) {
                console.error(e);
                casesContainer.innerHTML = `<div style="color: var(--accent-red); font-size: 0.82rem; text-align: center; margin-top: 2rem;">Failed to load test scenarios.</div>`;
            }
        }



        // Create new project from Tab 3 Project Console inputs
        async function createProjectFromConsole() {
            const input = document.getElementById('consoleProjectNameInput');
            const name = input.value.trim();
            if (!name) return alert("Please enter a project name.");

            try {
                const response = await fetch(`/create-project?project_name=${encodeURIComponent(name)}`, { method: 'POST' });
                if (response.ok) {
                    input.value = '';
                    await fetchProjects(name);
                    await loadProjectConsoleStats();
                    alert(`Project '${name}' successfully created!`);
                } else {
                    alert("Failed to create project");
                }
            } catch (e) {
                console.error(e);
            }
        }

        // Create empty flow module from Tab 3 Project Console inputs
        async function createEmptyFlowFromConsole() {
            const nameInput = document.getElementById('consoleFlowNameInput');
            const urlInput = document.getElementById('consoleFlowUrlInput');
            const name = nameInput.value.trim();
            const url = urlInput.value.trim();

            if (!name || !url) return alert("Flow Name and Start URL are required.");

            try {
                const response = await fetch(`/create-empty-flow?flow_name=${encodeURIComponent(name)}&url=${encodeURIComponent(url)}&project_name=${encodeURIComponent(currentProjectName)}`, { method: 'POST' });
                if (response.ok) {
                    nameInput.value = '';
                    await fetchSavedFlows();
                    await loadProjectConsoleStats();
                    alert(`Empty flow module '${name}' registered successfully!`);
                } else {
                    alert("Failed to create flow");
                }
            } catch (e) {
                console.error(e);
            }
        }

        // Helper to run AI Analysis from the Tab 3 checklist table actions
        async function triggerConsoleAIAnalysis(flowName, btn) {
            const row = btn.closest('tr');
            const originalHTML = row.innerHTML;
            row.innerHTML = `<td colspan="5" style="padding: 0.75rem 1rem; text-align:center; color: var(--accent-yellow); font-weight: 500;">⚡ Gemini Architect is generating test scenarios for '${flowName}'... Please wait.</td>`;
            try {
                const response = await fetch(`/analyze-flow?flow_name=${encodeURIComponent(flowName)}&project_name=${encodeURIComponent(currentProjectName)}`, { method: 'POST' });
                if (response.ok) {
                    await loadProjectConsoleStats();
                    alert(`AI analysis complete. Test scenarios generated successfully for '${flowName}'!`);
                } else {
                    alert("AI analysis failed.");
                    row.innerHTML = originalHTML;
                }
            } catch (e) {
                console.error(e);
                row.innerHTML = originalHTML;
            }
        }

        // Confirm selected project from lander screen
        async function confirmLanderSelect() {
            const select = document.getElementById('landerProjectSelect');
            if (!select.value) {
                alert("Please select a project or create one first!");
                return;
            }
            currentProjectName = select.value;
            document.getElementById('projectSelect').value = currentProjectName;
            document.getElementById('projectLander').style.display = 'none';
            await switchProject();
        }

        // Create a project from lander screen
        async function createProjectFromLander() {
            const input = document.getElementById('landerNewProjectName');
            const name = input.value.trim();
            if (!name) {
                alert("Please enter a project name.");
                return;
            }

            try {
                const response = await fetch(`/create-project?project_name=${encodeURIComponent(name)}`, {
                    method: 'POST'
                });
                if (response.ok) {
                    currentProjectName = name;
                    document.getElementById('projectLander').style.display = 'none';
                    await fetchProjects(name);
                } else {
                    alert("Failed to create project");
                }
            } catch (e) {
                console.error(e);
            }
        }

        // Fetch Projects List in workspace on load
        async function fetchProjects(selectProjectName = "") {
            try {
                const response = await fetch('/list-projects', { cache: "no-store" });
                const data = await response.json();

                // Populate lander select list
                const landerSelect = document.getElementById('landerProjectSelect');
                landerSelect.innerHTML = '';

                // Populate header select list
                const headerSelect = document.getElementById('projectSelect');
                headerSelect.innerHTML = '';
                
                // Populate explorer select list
                const explorerSelect = document.getElementById('explorerProjectSelect');
                if (explorerSelect) explorerSelect.innerHTML = '';

                data.projects.forEach(proj => {
                    const opt1 = document.createElement('option');
                    opt1.value = proj; opt1.innerText = proj;
                    landerSelect.appendChild(opt1);

                    const opt2 = document.createElement('option');
                    opt2.value = proj; opt2.innerText = proj;
                    headerSelect.appendChild(opt2);
                    
                    if (explorerSelect) {
                        const opt3 = document.createElement('option');
                        opt3.value = proj; opt3.innerText = proj;
                        explorerSelect.appendChild(opt3);
                    }
                });

                if (selectProjectName) {
                    landerSelect.value = selectProjectName;
                    headerSelect.value = selectProjectName;
                    if (explorerSelect) explorerSelect.value = selectProjectName;
                    currentProjectName = selectProjectName;
                    await switchProject();
                } else {
                    // Pre-select active project in explorer dropdown by default
                    if (explorerSelect) {
                        explorerSelect.value = currentProjectName;
                        loadExplorerFlowsList();
                    }
                }
            } catch (e) {
                console.error(e);
            }
        }

        // Project Switching trigger
        async function switchProject() {
            currentProjectName = document.getElementById('projectSelect').value;
            resetAIWorkspace();
            document.getElementById('flowDetailsCard').style.display = 'none';
            await fetchSavedFlows();
        }

        // Spawn prompt input to create a project folder
        async function spawnCreateProjectPrompt() {
            const name = prompt("Enter a name for the new independent project:");
            if (!name || !name.trim()) return;

            try {
                const response = await fetch(`/create-project?project_name=${encodeURIComponent(name.trim())}`, {
                    method: 'POST'
                });
                if (response.ok) {
                    await fetchProjects(name.trim());
                } else {
                    alert("Failed to create project");
                }
            } catch (e) {
                console.error(e);
            }
        }

        // Fetch flows from project directory
        async function fetchSavedFlows(selectFlowName = "") {
            try {
                const response = await fetch(`/list-flows?project_name=${encodeURIComponent(currentProjectName)}`, { cache: "no-store" });
                const data = await response.json();

                globalAuthFlows = data.auth_flows || [];
                globalBusinessFlows = data.business_flows || [];

                const select = document.getElementById('flowSelect');
                const authGroup = document.getElementById('authFlowsGroup');
                const bizGroup = document.getElementById('businessFlowsGroup');

                authGroup.innerHTML = '';
                bizGroup.innerHTML = '';

                // Populate Authentication optgroup
                data.auth_flows.forEach(flow => {
                    const opt = document.createElement('option');
                    opt.value = flow;
                    opt.innerText = flow;
                    authGroup.appendChild(opt);
                });

                // Populate Business optgroup
                data.business_flows.forEach(flow => {
                    const opt = document.createElement('option');
                    opt.value = flow;
                    opt.innerText = flow;
                    bizGroup.appendChild(opt);
                });

                if (selectFlowName) {
                    select.value = selectFlowName;
                    loadSelectedFlow();
                }

                // Also update the mapped auth dropdown selector list inside the config box
                const authMapSelect = document.getElementById('authFlowSelect');
                authMapSelect.innerHTML = '<option value="">-- No Auth Flow --</option>';
                data.auth_flows.forEach(flow => {
                    const opt = document.createElement('option');
                    opt.value = flow;
                    opt.innerText = flow;
                    authMapSelect.appendChild(opt);
                });

                if (data.auth_flows.includes('login')) {
                    authMapSelect.value = 'login';
                }

                await loadAuthScenarios();
                renderDependencyOptions();
            } catch (e) {
                console.error(e);
            }
        }

        // Populate Auth Scenarios based on selected Auth Flow
        async function loadAuthScenarios() {
            const authFlow = document.getElementById('authFlowSelect').value;
            const select = document.getElementById('authScenarioSelect');
            select.innerHTML = '<option value="">-- No Auth Scenario --</option>';

            if (!authFlow) return;

            try {
                const response = await fetch(`/get-flow-details?flow_name=${encodeURIComponent(authFlow)}&project_name=${encodeURIComponent(currentProjectName)}`, { cache: "no-store" });
                if (!response.ok) return;

                const data = await response.json();
                if (!data.has_test_cases) {
                    select.innerHTML = '<option value="">-- Run AI Analysis on Auth Flow First --</option>';
                    return;
                }

                select.innerHTML = '';
                let defaultIndex = 0;

                data.test_cases.forEach((tc, idx) => {
                    const tcName = tc.scenario_name || tc.scenario;
                    const opt = document.createElement('option');
                    opt.value = tcName;
                    opt.innerText = tcName;
                    select.appendChild(opt);

                    const searchName = tcName.toLowerCase();
                    if (searchName.includes('positive') || searchName.includes('valid credentials') || searchName.includes('successful login')) {
                        defaultIndex = idx;
                    }
                });

                select.selectedIndex = defaultIndex;
            } catch (e) {
                console.error(e);
            }
        }

        // Fetch flow details and test cases when flow selection changes
        async function loadSelectedFlow() {
            const flowName = document.getElementById('flowSelect').value;
            currentFlowName = flowName;

            if (!flowName) {
                document.getElementById('analyzeBtn').disabled = true;
                document.getElementById('multiAgentBtn').disabled = true;
                document.getElementById('flowDetailsCard').style.display = 'none';
                resetAIWorkspace();
                return;
            }

            try {
                const response = await fetch(`/get-flow-details?flow_name=${encodeURIComponent(flowName)}&project_name=${encodeURIComponent(currentProjectName)}`, { cache: "no-store" });
                if (!response.ok) throw new Error("Failed to load details");

                const data = await response.json();
                flowMetadata = data.flow;
                flowSuggestions = data.suggestions || {};

                // Show metadata card
                document.getElementById('metaUrl').innerText = data.flow.target_url;
                document.getElementById('metaSteps').innerText = `${data.flow.step_count} steps`;
                document.getElementById('metaDate').innerText = data.flow.timestamp;
                
                if (data.has_recording && data.recording_url) {
                    document.getElementById('metaRecordingContainer').style.display = 'inline-flex';
                    document.getElementById('watchRecordingBtn').onclick = () => openVideoModal(data.recording_url);
                } else {
                    document.getElementById('metaRecordingContainer').style.display = 'none';
                }
                
                document.getElementById('flowDetailsCard').style.display = 'block';

                // Set default Execution Profile parameters
                const isAuthFlow = ["login", "auth", "session", "logout"].some(kw => flowName.toLowerCase().includes(kw));
                document.getElementById('profileAuthReq').value = isAuthFlow ? 'no' : 'yes';

                // Render dependency multi-select options
                renderDependencyOptions();

                // Populate Enterprise QA Specification if present
                const qaPanel = document.getElementById('enterpriseQAPanel');
                if (data.has_spec && data.spec) {
                    const spec = data.spec;
                    qaPanel.style.display = 'block';

                    // Set readiness
                    const readyBadge = document.getElementById('specReadinessBadge');
                    readyBadge.innerText = (spec.release_readiness || "PENDING").toUpperCase();
                    if ((spec.release_readiness || "").toLowerCase().includes("ready") && !(spec.release_readiness || "").toLowerCase().includes("not")) {
                        readyBadge.className = "test-badge btn-green";
                    } else {
                        readyBadge.className = "test-badge btn-red";
                    }

                    document.getElementById('specUnderstanding').innerText = spec.requirement_understanding || "-";

                    // Helper to build list
                    const buildList = (listId, array) => {
                        const el = document.getElementById(listId);
                        el.innerHTML = '';
                        const items = array || [];
                        if (items.length === 0) {
                            el.innerHTML = `<li style="color: var(--text-muted); list-style-type: none;">No items identified.</li>`;
                        } else {
                            items.forEach(item => {
                                el.innerHTML += `<li>${item}</li>`;
                            });
                        }
                    };

                    buildList('specGaps', spec.requirement_gaps);
                    buildList('specRules', spec.business_rules);
                    buildList('specRisks', spec.risks);
                    buildList('specUIVal', spec.ui_validation);
                    buildList('specAPIVal', spec.api_validation);
                    buildList('specDBVal', spec.database_validation);
                    buildList('specSecVal', spec.security_validation);
                    buildList('specPerfVal', spec.performance_validation);
                    buildList('specAccessVal', spec.accessibility_validation);
                    buildList('specRegression', spec.regression_impact);
                    buildList('specAutomation', spec.automation_candidates);
                    buildList('specSuggestions', spec.defect_prevention_suggestions);

                    // Strategy object formatting
                    const strat = spec.test_strategy || {};
                    document.getElementById('specStrategy').innerHTML = `
                        <div><b>Objectives:</b> ${strat.objectives || "-"}</div>
                        <div style="margin-top:0.25rem;"><b>Scope In:</b> ${strat.scope_in || "-"}</div>
                        <div style="margin-top:0.25rem;"><b>Scope Out:</b> ${strat.scope_out || "-"}</div>
                        <div style="margin-top:0.25rem;"><b>Rollback Plan:</b> ${strat.rollback_plan || "-"}</div>
                    `;

                    document.getElementById('specCoverage').innerText = spec.coverage_summary || "-";
                    document.getElementById('specSelfReview').innerText = spec.ai_self_review || "-";

                    // Default to showing first tab
                    switchSpecTab('spec-req', document.querySelector('.btn-tab'));
                } else {
                    qaPanel.style.display = 'none';
                }

                renderTimeline(data.actions);
                document.getElementById('analyzeBtn').disabled = false;
                document.getElementById('multiAgentBtn').disabled = false;

                // If test cases already exist, load configuration panel and display cases
                if (data.has_test_cases) {
                    document.getElementById('configBox').style.display = 'flex';
                    renderSuiteList(data.test_cases);
                } else {
                    document.getElementById('configBox').style.display = 'none';
                    document.getElementById('suiteList').innerHTML = `
                        <div style="color: var(--text-muted); text-align: center; padding: 2.5rem; font-size:0.9rem;">
                            No test cases generated yet. Click 'Run AI Post Analysis' to generate all test scenarios!
                        </div>
                    `;
                }
                // Populate data-matrix options for data-driven execution
                const matrixSelect = document.getElementById('dataMatrixSelect');
                if (matrixSelect) {
                    matrixSelect.innerHTML = '<option value="">None</option>';
                    (data.data_files || []).forEach(name => {
                        const option = document.createElement('option');
                        option.value = name;
                        option.innerText = name;
                        matrixSelect.appendChild(option);
                    });
                }

                await loadEnvironmentOptions();
                await loadApiSeedOptions();

                if (data.has_report && data.latest_report) {
                    restoreExecutionReport(data.latest_report);
                } else {
                    document.getElementById('reportCard').style.display = 'none';
                }
                document.getElementById('terminalConsole').style.display = 'none';

            } catch (e) {
                alert(e.message);
            }
        }

        function resetAIWorkspace() {
            document.getElementById('configBox').style.display = 'none';
            document.getElementById('reportCard').style.display = 'none';
            document.getElementById('terminalConsole').style.display = 'none';
            if (document.getElementById('enterpriseQAPanel')) {
                document.getElementById('enterpriseQAPanel').style.display = 'none';
            }
            document.getElementById('suiteList').innerHTML = `
                <div style="color: var(--text-muted); text-align: center; padding: 2.5rem; font-size:0.9rem;">
                    Select a recorded business flow from the library dropdown to load or generate AI test cases!
                </div>
            `;
        }

        // Toggle Active Flow details collapse state
        function toggleFlowDetails() {
            const content = document.getElementById('flowDetailsContent');
            const icon = document.getElementById('detailsToggleIcon');
            if (content.style.display === 'none') {
                content.style.display = 'flex';
                icon.innerText = '▼ Hide Details';
            } else {
                content.style.display = 'none';
                icon.innerText = '▲ Show Details';
            }
        }

        // Render the action timeline under flow library
        function renderTimeline(actions) {
            const container = document.getElementById('timelineContainer');
            container.innerHTML = '';

            actions.forEach((act, idx) => {
                const card = document.createElement('div');
                card.className = 'step-card';

                const actionBadgeClass = act.action === 'CLICK' ? 'action-click' : 'action-type';
                const label = act.action === 'CLICK'
                    ? `Clicked on element (${act.tag || 'el'})`
                    : `Typed value "${act.value}"`;

                card.innerHTML = `
                    <div class="step-index">${idx + 1}</div>
                    <div class="step-details">
                        <div style="display: flex; gap: 0.5rem; align-items: center;">
                            <span class="step-action-badge ${actionBadgeClass}">${act.action}</span>
                            <span style="font-weight: 500;">${label}</span>
                        </div>
                        <div class="step-selector">${act.selector}</div>
                    </div>
                    ${act.screenshot ? `
                        <div class="step-thumbnail" onclick="openLightbox('${act.screenshot}')">
                            <img src="${act.screenshot}" alt="Step ${idx + 1}">
                        </div>
                    ` : ''}
                `;
                container.appendChild(card);
            });
        }

        // Render generated test case cards
        // Cached suite for search / editing / selective execution
        let loadedTestCases = [];

        function escapeHtml(text) {
            return String(text === undefined || text === null ? '' : text)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;');
        }

        function renderSuiteList(cases) {
            loadedTestCases = cases || [];
            const container = document.getElementById('suiteList');
            const withoutAssertions = loadedTestCases.filter(tc => !(tc.steps || []).some(s => {
                const action = typeof s === 'string' ? s : (s.action || '');
                return String(action).toUpperCase().startsWith('VERIFY') ||
                       String(action).toUpperCase().startsWith('ASSERT');
            })).length;

            container.innerHTML = `
                <div style="border-bottom: 1px solid var(--border-color); padding-bottom: 0.5rem; margin-bottom: 0.5rem;">
                    <div style="display:flex; justify-content:space-between; align-items:center; gap:0.5rem; flex-wrap:wrap;">
                        <div style="font-weight:600; font-size: 0.95rem;">
                            🤖 Total Generated Test Cases: <span style="color:#60a5fa;">${loadedTestCases.length} Scenarios</span>
                        </div>
                        <div style="display:flex; gap:0.4rem; align-items:center; flex-wrap:wrap;">
                            <input id="suiteSearchInput" type="search" placeholder="Search scenarios / selectors..."
                                oninput="applySuiteFilter()"
                                style="padding:0.35rem 0.5rem; font-size:0.78rem; background:rgba(0,0,0,0.35); border:1px solid var(--border-color); color:white; border-radius:4px; width:210px;">
                            <select id="suiteTypeFilter" onchange="applySuiteFilter()"
                                style="padding:0.35rem; font-size:0.78rem;">
                                <option value="">All types</option>
                            </select>
                            <button class="btn" style="background:#334155; font-size:0.72rem; padding:0.3rem 0.6rem;"
                                onclick="toggleSelectAllCases()">☑ Select all</button>
                            <button class="btn" style="background:#0f766e; font-size:0.72rem; padding:0.3rem 0.6rem;"
                                onclick="openCaseEditor(0)">➕ Add scenario</button>
                        </div>
                    </div>
                    ${withoutAssertions ? `<div style="margin-top:0.4rem; font-size:0.75rem; color:#fbbf24;">
                        ⚠️ ${withoutAssertions} scenario(s) contain no VERIFY step, so the verdict engine can only report
                        them as UNVERIFIED. Use ✏️ Edit to add an assertion, or re-run Multi-Agent generation.</div>` : ''}
                    <div id="suiteFilterInfo" style="margin-top:0.35rem; font-size:0.72rem; color:var(--text-muted);"></div>
                </div>
            `;

            loadedTestCases.forEach((tc, idx) => {
                const number = idx + 1;
                const card = document.createElement('div');
                card.className = 'test-case-card';
                card.id = `suiteCard-${number}`;
                card.dataset.type = tc.type || '';
                card.dataset.index = number;

                let stepsHtml = '';
                (tc.steps || []).forEach(s => {
                    if (typeof s === 'string') {
                        stepsHtml += `<li>${escapeHtml(s)}</li>`;
                    } else {
                        const isAssert = String(s.action || '').toUpperCase().startsWith('VERIFY') ||
                                         String(s.action || '').toUpperCase().startsWith('ASSERT');
                        stepsHtml += `<li${isAssert ? ' style="color:#7dd3fc;"' : ''}><b>${escapeHtml(s.action)}</b>` +
                            (s.selector ? ` on <code>${escapeHtml(s.selector)}</code>` : '') +
                            (s.value ? ` with value <i>"${escapeHtml(s.value)}"</i>` : '') + `</li>`;
                    }
                });

                const tcName = tc.scenario_name || tc.scenario || `Scenario #${number}`;
                const tcExpected = tc.expected_result || tc.expected || '';

                card.innerHTML = `
                    <div class="test-case-header">
                        <span style="display:flex; align-items:center; gap:0.45rem;">
                            <input type="checkbox" class="caseSelect" value="${number}"
                                onchange="updateSelectionUi()" title="Select for 'Run Selected Only'">
                            <span>#${number}: ${escapeHtml(tcName)}</span>
                        </span>
                        <span style="display:flex; align-items:center; gap:0.4rem;">
                            <span class="test-badge badge-${tc.type}">${escapeHtml(tc.type)}</span>
                            <button class="btn" style="background:#334155; font-size:0.68rem; padding:0.15rem 0.45rem;"
                                onclick="openCaseEditor(${number})" title="Edit this scenario">✏️</button>
                            <button class="btn" style="background:#334155; font-size:0.68rem; padding:0.15rem 0.45rem;"
                                onclick="runSingleCase(${number})" title="Run only this scenario">▶</button>
                            <button class="btn btn-red" style="font-size:0.68rem; padding:0.15rem 0.45rem;"
                                onclick="deleteCase(${number})" title="Delete this scenario">🗑</button>
                        </span>
                    </div>
                    <ul style="font-size:0.8rem; color: var(--text-muted); padding-left: 1.25rem;">
                        ${stepsHtml}
                    </ul>
                    <div style="font-size: 0.8rem; color: #a7f3d0;">🔮 Expected: ${escapeHtml(tcExpected)}</div>
                    <div id="suiteCardStatus-${number}" style="font-size:0.8rem; font-weight:600; color:var(--text-muted); margin-top: 0.15rem;">Status: AWAITING RUN</div>
                    <div id="suiteCardVerdict-${number}" style="font-size:0.75rem; color:var(--text-muted); margin-top:0.1rem;"></div>
                `;
                container.appendChild(card);
            });

            // Populate the type filter from the loaded suite
            const typeFilter = document.getElementById('suiteTypeFilter');
            if (typeFilter) {
                const types = [...new Set(loadedTestCases.map(tc => tc.type).filter(Boolean))].sort();
                types.forEach(t => {
                    const option = document.createElement('option');
                    option.value = t;
                    option.innerText = t;
                    typeFilter.appendChild(option);
                });
            }
            updateSelectionUi();
        }

        function applySuiteFilter() {
            const needle = (document.getElementById('suiteSearchInput')?.value || '').toLowerCase();
            const type = document.getElementById('suiteTypeFilter')?.value || '';
            let shown = 0;
            loadedTestCases.forEach((tc, idx) => {
                const card = document.getElementById(`suiteCard-${idx + 1}`);
                if (!card) return;
                const haystack = JSON.stringify(tc).toLowerCase();
                const matches = (!needle || haystack.includes(needle)) &&
                                (!type || (tc.type || '') === type);
                card.style.display = matches ? '' : 'none';
                if (matches) shown++;
            });
            const info = document.getElementById('suiteFilterInfo');
            if (info) {
                info.innerText = (needle || type)
                    ? `Showing ${shown} of ${loadedTestCases.length} scenarios.` : '';
            }
        }

        function toggleSelectAllCases() {
            const boxes = [...document.querySelectorAll('.caseSelect')]
                .filter(cb => cb.closest('.test-case-card').style.display !== 'none');
            const allChecked = boxes.every(cb => cb.checked);
            boxes.forEach(cb => { cb.checked = !allChecked; });
            updateSelectionUi();
        }

        function selectedCaseIds() {
            return [...document.querySelectorAll('.caseSelect:checked')].map(cb => cb.value);
        }

        function updateSelectionUi() {
            const count = selectedCaseIds().length;
            const button = document.getElementById('runSelectedBtn');
            if (button) {
                button.style.display = count ? 'inline-block' : 'none';
                button.innerText = `▶ Run Selected Only (${count})`;
            }
        }

        // Launch headed playwright browser
        async function triggerLaunchBrowser() {
            const flowName = document.getElementById('flowNameInput').value.trim();
            const url = document.getElementById('targetUrlInput').value.trim();
            if (!flowName || !url) return alert("Flow Name and URL are required.");

            logConsole("Launching browser page...");
            try {
                const response = await fetch(`/create-flow?flow_name=${encodeURIComponent(flowName)}&url=${encodeURIComponent(url)}&project_name=${encodeURIComponent(currentProjectName)}`, { method: 'POST' });
                const data = await response.json();
                logConsole(`✅ Browser ready. ${data.message}`);

                document.getElementById('startRecBtn').disabled = false;
                document.getElementById('stopRecBtn').disabled = false;
                document.getElementById('launchBtn').disabled = true;
            } catch (e) {
                logConsole(`🔴 Launch error: ${e.message}`, true);
            }
        }

        // Start event recording
        async function triggerStartRecording() {
            logConsole("Activating recording hooks...");
            try {
                await fetch('/start-recording', { method: 'POST' });
                logConsole("🔴 RECORDING... Go to the browser and execute your actions!");
                document.getElementById('startRecBtn').disabled = true;
            } catch (e) {
                logConsole(`🔴 Error starting: ${e.message}`, true);
            }
        }

        // Stop recording
        async function triggerStopRecording() {
            logConsole("Saving files and cleaning up session...");
            try {
                const response = await fetch('/stop-recording', { method: 'POST' });
                const data = await response.json();
                logConsole(`✅ Saved. ${data.message}`);

                document.getElementById('launchBtn').disabled = false;
                document.getElementById('startRecBtn').disabled = true;
                document.getElementById('stopRecBtn').disabled = true;

                // Refresh dropdown
                const flowName = document.getElementById('flowNameInput').value.trim();
                await fetchSavedFlows(flowName);
            } catch (e) {
                logConsole(`🔴 Stop error: ${e.message}`, true);
            }
        }

        // Trigger Gemini Post Analysis Guided by Checkbox Selections
        async function triggerAIAnalysis() {
            if (!currentFlowName) return;
            const loader = document.getElementById('analysisLoader');
            const list = document.getElementById('suiteList');
            const runBtn = document.getElementById('analyzeBtn');
            const config = document.getElementById('configBox');

            // Gather selected checkboxes for test types to guide Gemini generation
            const checkboxes = document.querySelectorAll('input[name="testType"]:checked');
            const types = Array.from(checkboxes).map(cb => cb.value).join(",");

            loader.style.display = 'flex';
            list.style.display = 'none';
            config.style.display = 'none';
            runBtn.disabled = true;

            try {
                const response = await fetch(`/analyze-flow?flow_name=${encodeURIComponent(currentFlowName)}&project_name=${encodeURIComponent(currentProjectName)}&test_types=${encodeURIComponent(types)}`, { method: 'POST' });
                if (!response.ok) throw new Error("AI analysis failed.");

                await loadSelectedFlow();
            } catch (e) {
                alert(e.message);
            } finally {
                loader.style.display = 'none';
                list.style.display = 'flex';
                runBtn.disabled = false;
            }
        }

        // Run custom execution matrix using Server-Sent Events
        let activeRunId = "";
        let activeEventSource = null;

        function triggerTestSuite(selectedOnly = false) {
            if (!currentFlowName) return alert("Select a business flow first.");
            let count = document.getElementById('caseCountSelect').value;
            if (count === "max") {
                count = -1;
            }
            const mode = document.getElementById('executionModeSelect').value;
            const terminal = document.getElementById('terminalConsole');
            const report = document.getElementById('reportCard');
            const startBtn = document.getElementById('runSuiteBtn');
            const stopBtn = document.getElementById('stopSuiteBtn');

            // Gather selected checkboxes for test types
            const checkboxes = document.querySelectorAll('input[name="testType"]:checked');
            const types = Array.from(checkboxes).map(cb => cb.value).join(",");

            // Read Execution Profile settings
            const authRequired = document.getElementById('profileAuthReq').value;
            const dependencies = document.getElementById('profileDependencies').value.trim();

            let authFlow = "";
            let authScenario = "";

            if (authRequired === 'yes') {
                authFlow = document.getElementById('authFlowSelect').value;
                authScenario = document.getElementById('authScenarioSelect').value;
            }

            // Verdict & performance options
            const verdictMode = document.getElementById('verdictModeSelect')?.value || 'rules';
            const workers = document.getElementById('workersSelect')?.value || '1';
            const stepShots = document.getElementById('stepShotsSelect')?.value || 'fail';
            const envName = document.getElementById('envSelect')?.value || '';
            const dataMatrix = document.getElementById('dataMatrixSelect')?.value || '';
            const apiSeedName = document.getElementById('apiSeedSelect')?.value || '';
            const tracing = document.getElementById('tracingCheck')?.checked ? 'true' : 'false';
            const depUnique = document.getElementById('depUniqueCheck')?.checked ? 'true' : 'false';
            const visual = document.getElementById('visualCheck')?.checked ? 'true' : 'false';
            const caseIds = selectedOnly ? selectedCaseIds().join(',') : '';

            if (selectedOnly && !caseIds) return alert("Tick at least one scenario first.");

            terminal.innerHTML = '';
            terminal.style.display = 'block';
            report.style.display = 'none';
            startBtn.disabled = true;
            if (stopBtn) stopBtn.style.display = 'inline-block';

            // Reset cards styling and statuses
            document.querySelectorAll('.test-case-card').forEach(card => {
                card.classList.remove('active');
                const number = card.dataset.index || card.id.split('-')[1];
                const statusEl = document.getElementById(`suiteCardStatus-${number}`);
                if (statusEl) {
                    statusEl.innerText = 'Status: AWAITING RUN';
                    statusEl.style.color = 'var(--text-muted)';
                }
                const verdictEl = document.getElementById(`suiteCardVerdict-${number}`);
                if (verdictEl) verdictEl.innerText = '';
            });

            logTerminal("Initiating test context...");

            // Construct dynamic stream query URL including mapped auth flow criteria, dependencies, and isolated project name
            const streamUrl = `/run-matrix-stream?flow_name=${encodeURIComponent(currentFlowName)}&num_cases=${count}&mode=${mode}&test_types=${encodeURIComponent(types)}&auth_flow=${encodeURIComponent(authFlow)}&auth_scenario=${encodeURIComponent(authScenario)}&dependencies=${encodeURIComponent(dependencies)}&project_name=${encodeURIComponent(currentProjectName)}` +
                `&verdict_mode=${verdictMode}&workers=${workers}&step_shots=${stepShots}&tracing=${tracing}` +
                `&dep_unique_data=${depUnique}&visual_check=${visual}&env_name=${encodeURIComponent(envName)}` +
                `&data_matrix=${encodeURIComponent(dataMatrix)}&api_seed_name=${encodeURIComponent(apiSeedName)}` +
                `&case_ids=${encodeURIComponent(caseIds)}`;
            const eventSource = new EventSource(streamUrl);
            activeEventSource = eventSource;

            let passedCount = 0;
            let failedCount = 0;
            let unverifiedCount = 0;
            let totalExecuted = 0;
            let totalDuration = 0;
            let allConsoleErrors = [];
            let allNetworkErrors = [];
            const verdictNotes = [];
            const screenshotGrid = document.getElementById('screenshotGrid');
            screenshotGrid.innerHTML = '';

            eventSource.onmessage = function (event) {
                const data = JSON.parse(event.data);

                if (data.run_id && data.run_id !== activeRunId) {
                    activeRunId = data.run_id;
                    const badge = document.getElementById('runIdBadge');
                    if (badge) badge.innerText = `run ${activeRunId}`;
                }

                if (data.event === 'info' || data.event === 'log') {
                    logTerminal(data.message);
                } else if (data.event === 'start') {
                    logTerminal(data.message, '#3b82f6');
                } else if (data.event === 'error') {
                    logTerminal(`⚠️ ${data.message}`, '#f59e0b');
                } else if (data.event === 'cancelled') {
                    logTerminal(data.message, '#f59e0b');
                } else if (data.event === 'test_start') {
                    const seqInfo = (data.run_seq && data.total_run) ? ` (${data.run_seq}/${data.total_run})` : '';
                    logTerminal(`🎬 [Running Test #${data.index}${seqInfo}] ${data.name}...`, '#f59e0b');
                    const card = document.getElementById(`suiteCard-${data.index}`);
                    if (card) {
                        card.classList.add('active');
                        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    }
                    const statusText = document.getElementById(`suiteCardStatus-${data.index}`);
                    if (statusText) {
                        statusText.innerText = 'Status: EXECUTING...';
                        statusText.style.color = '#f59e0b';
                    }
                } else if (data.event === 'test_end') {
                    totalExecuted++;
                    totalDuration += data.duration;

                    const card = document.getElementById(`suiteCard-${data.index}`);
                    if (card) card.classList.remove('active');

                    const statusColour = {
                        PASS: 'var(--accent-green)', FAIL: 'var(--accent-red)',
                        UNVERIFIED: '#f59e0b', CRASH: '#f87171', CANCELLED: 'var(--text-muted)'
                    }[data.status] || 'var(--text-muted)';

                    const statusText = document.getElementById(`suiteCardStatus-${data.index}`);
                    if (statusText) {
                        statusText.innerText = `Status: ${data.status} (Completed in ${data.duration}s)`;
                        statusText.style.color = statusColour;
                    }

                    const reason = (data.verdict && data.verdict.reason) || data.error || '';
                    const verdictEl = document.getElementById(`suiteCardVerdict-${data.index}`);
                    if (verdictEl && reason) {
                        verdictEl.innerHTML = `⚖️ ${escapeHtml(reason)}`;
                        verdictEl.style.color = statusColour;
                    }

                    if (data.status === 'PASS') {
                        passedCount++;
                        logTerminal(`🟢 [Test #${data.index} PASS] ${reason}`, '#10b981');
                    } else if (data.status === 'UNVERIFIED') {
                        unverifiedCount++;
                        verdictNotes.push({ index: data.index, name: data.name, status: data.status, reason });
                        logTerminal(`🟡 [Test #${data.index} UNVERIFIED] ${reason}`, '#f59e0b');
                    } else if (data.status === 'CANCELLED') {
                        logTerminal(`⏹️ [Test #${data.index} CANCELLED]`, '#94a3b8');
                    } else {
                        failedCount++;
                        verdictNotes.push({ index: data.index, name: data.name, status: data.status, reason });
                        logTerminal(`🔴 [Test #${data.index} ${data.status}] ${reason || 'Assertion failed'}`, '#ef4444');
                    }

                    if (data.messages && data.messages.length) {
                        logTerminal(`   🔍 On-screen messages: ${data.messages.map(m => m.text).join(' | ')}`, '#93c5fd');
                    }
                    if (data.downloads && data.downloads.length) {
                        logTerminal(`   📥 Downloaded: ${data.downloads.map(d => `${d.suggested_filename} (${d.row_count ?? '?'} rows)`).join(', ')}`, '#93c5fd');
                    }
                    if (data.progress) {
                        logTerminal(`   ▸ Progress: ${data.progress.completed}/${data.progress.total}`, '#64748b');
                    }

                    // Accumulate errors
                    if (data.console_errors && data.console_errors.length > 0) {
                        allConsoleErrors = allConsoleErrors.concat(data.console_errors);
                    }
                    if (data.network_errors && data.network_errors.length > 0) {
                        allNetworkErrors = allNetworkErrors.concat(data.network_errors);
                    }

                    // Add checkpoint screenshot thumbnail
                    const thumb = document.createElement('div');
                    thumb.className = 'screenshot-card';
                    thumb.style.position = 'relative';
                    
                    let videoOverlay = '';
                    if (data.video) {
                        videoOverlay = `
                            <div class="video-play-icon" onclick="event.stopPropagation(); openVideoModal('${data.video}')" 
                                 style="position: absolute; top: 8px; right: 8px; background: #10b981; color: white; border-radius: 50%; width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; font-size: 0.8rem; cursor: pointer; z-index: 10;" 
                                 title="Watch Video Run">
                                ▶
                            </div>
                        `;
                    }
                    
                    thumb.onclick = () => openLightbox(data.screenshot);
                    thumb.innerHTML = `
                        ${videoOverlay}
                        <img src="${data.screenshot}" alt="Check ${data.index}">
                        <div class="screenshot-label">Case #${data.index} (${data.status})</div>
                    `;
                    screenshotGrid.appendChild(thumb);

                } else if (data.event === 'complete') {
                    logTerminal(`🏁 ${data.message}`, '#10b981');
                    eventSource.close();
                    activeEventSource = null;
                    startBtn.disabled = false;
                    if (stopBtn) stopBtn.style.display = 'none';

                    // Compile final visual report card
                    renderFinalReport(passedCount, failedCount, totalExecuted, totalDuration,
                        allConsoleErrors, allNetworkErrors, types, unverifiedCount, verdictNotes);
                }
            };

            eventSource.onerror = function () {
                logTerminal("⚠️ Connection error or runner stream ended.", "#ef4444");
                eventSource.close();
                activeEventSource = null;
                startBtn.disabled = false;
                if (stopBtn) stopBtn.style.display = 'none';
            };
        }

        // Stop a running suite (server-side cancel, then close the stream)
        async function stopExecution() {
            const stopBtn = document.getElementById('stopSuiteBtn');
            if (stopBtn) stopBtn.disabled = true;
            logTerminal("⏹ Stop requested — finishing the current scenario and saving a partial report...", '#f59e0b');
            try {
                const query = activeRunId
                    ? `run_id=${encodeURIComponent(activeRunId)}`
                    : `project_name=${encodeURIComponent(currentProjectName)}&flow_name=${encodeURIComponent(currentFlowName)}`;
                const response = await fetch(`/stop-run?${query}`, { method: 'POST' });
                const data = await response.json();
                logTerminal(`⏹ ${data.message || data.detail}`, '#f59e0b');
            } catch (e) {
                logTerminal(`⚠️ Could not stop the run: ${e.message}`, '#ef4444');
            } finally {
                if (stopBtn) stopBtn.disabled = false;
            }
        }

        function runSingleCase(number) {
            document.querySelectorAll('.caseSelect').forEach(cb => { cb.checked = cb.value === String(number); });
            updateSelectionUi();
            triggerTestSuite(true);
        }

        function toggleAdvancedOptions() {
            const panel = document.getElementById('advancedOptions');
            const icon = document.getElementById('advancedToggleIcon');
            const open = panel.style.display !== 'none';
            panel.style.display = open ? 'none' : 'grid';
            if (icon) icon.innerText = open ? '▼' : '▲';
        }

        // Render final test execution report card
        function renderFinalReport(passed, failed, executed, duration, consoleErrors, networkErrors,
                                   types, unverified = 0, verdictNotes = []) {
            document.getElementById('statExecuted').innerText = executed;
            document.getElementById('statPassed').innerText = passed;
            document.getElementById('statFailed').innerText = failed;
            document.getElementById('statUnverified').innerText = unverified;
            document.getElementById('statDuration').innerText = `${duration}s`;

            document.getElementById('reportFlowName').innerText = currentFlowName;
            document.getElementById('reportDateTime').innerText = new Date().toLocaleString();

            // Format selected types
            const typesLabel = types.split(',').map(t => t.toUpperCase()).join(', ') || 'ALL SCENARIOS';
            document.getElementById('reportTypes').innerText = typesLabel;

            // Set main pass/fail status badge
            const badge = document.getElementById('reportStatusBadge');
            if (failed > 0) {
                badge.innerText = 'FAIL';
                badge.className = 'test-badge btn-red';
            } else if (unverified > 0) {
                badge.innerText = 'UNVERIFIED';
                badge.className = 'test-badge';
                badge.style.background = '#d97706';
            } else {
                badge.innerText = 'PASS';
                badge.className = 'test-badge btn-green';
            }

            renderVerdictNotes(verdictNotes);

            // Display errors section if failures exist
            const errSection = document.getElementById('errorSection');
            const errList = document.getElementById('errorLogList');
            errList.innerHTML = '';

            const totalErrors = consoleErrors.length + networkErrors.length;
            if (totalErrors > 0) {
                errSection.style.display = 'flex';
                consoleErrors.forEach(err => errList.innerHTML += `<div>[Console] ${err}</div>`);
                networkErrors.forEach(err => errList.innerHTML += `<div>[Network] ${err}</div>`);
            } else {
                errSection.style.display = 'none';
            }

            // Render AI Suggestions & recommendations
            const recList = document.getElementById('recommendationList');
            recList.innerHTML = '';

            const recs = flowSuggestions.recommendations || [
                "Implement input length boundaries to prevent layout displacement.",
                "Inject duplicate check validations on employee IDs.",
                "Ensure SSL redirects are enforced on login page."
            ];

            recs.forEach(rec => recList.innerHTML += `<li>${rec}</li>`);

            // Slide in the report card
            document.getElementById('reportCard').style.display = 'flex';
            document.getElementById('reportCard').scrollIntoView({ behavior: 'smooth' });
        }

        // Inline placeholder so a scenario without a screenshot does not 404
        const NO_SHOT_PLACEHOLDER = 'data:image/svg+xml;utf8,' + encodeURIComponent(
            `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180">
               <rect width="320" height="180" fill="#0f172a"/>
               <text x="160" y="95" fill="#475569" font-family="Arial" font-size="13"
                     text-anchor="middle">no screenshot captured</text></svg>`);

        // Render the "why did this not pass" list under the report header
        function renderVerdictNotes(notes) {
            const section = document.getElementById('verdictSection');
            const list = document.getElementById('verdictLogList');
            if (!section || !list) return;
            list.innerHTML = '';
            if (!notes || !notes.length) {
                section.style.display = 'none';
                return;
            }
            section.style.display = 'flex';
            notes.forEach(note => {
                const colour = note.status === 'UNVERIFIED' ? '#fbbf24' : '#fca5a5';
                list.innerHTML += `<div style="color:${colour}">[#${note.index} ${note.status}] ` +
                    `${escapeHtml(note.name || '')} — ${escapeHtml(note.reason || '')}</div>`;
            });
        }

        // Restore historical execution report card metrics, thumbnails, and play overlay buttons on reload
        function restoreExecutionReport(latestReport) {
            if (!latestReport) return;
            document.getElementById('statExecuted').innerText = latestReport.total_test_cases || (latestReport.passed_count + latestReport.failed_count);
            document.getElementById('statPassed').innerText = latestReport.passed_count;
            document.getElementById('statFailed').innerText = latestReport.failed_count;
            document.getElementById('statUnverified').innerText = latestReport.unverified_count || 0;
            document.getElementById('statDuration').innerText = `${latestReport.duration_seconds || 0}s`;

            document.getElementById('reportFlowName').innerText = latestReport.flow_name || currentFlowName;
            document.getElementById('reportDateTime').innerText = latestReport.execution_date || '';

            // Format selected types
            const rawTypes = latestReport.test_types_run;
            const typesLabel = Array.isArray(rawTypes) ? rawTypes.map(t => t.toUpperCase()).join(', ') : (rawTypes || 'ALL SCENARIOS').toUpperCase();
            document.getElementById('reportTypes').innerText = typesLabel;

            const badge = document.getElementById('reportStatusBadge');
            if (latestReport.failed_count > 0 || latestReport.crash_count > 0) {
                badge.innerText = 'FAIL';
                badge.className = 'test-badge btn-red';
            } else if (latestReport.unverified_count > 0) {
                badge.innerText = 'UNVERIFIED';
                badge.className = 'test-badge';
                badge.style.background = '#d97706';
            } else {
                badge.innerText = 'PASS';
                badge.className = 'test-badge btn-green';
            }

            // Restore per-scenario verdicts onto the suite cards and the notes list
            const notes = [];
            (latestReport.checkpoints || []).forEach(cp => {
                const reason = (cp.verdict && cp.verdict.reason) || cp.error || '';
                if (cp.status !== 'PASS' && reason) {
                    notes.push({ index: cp.index, name: cp.name, status: cp.status, reason });
                }
                const statusEl = document.getElementById(`suiteCardStatus-${cp.index}`);
                const verdictEl = document.getElementById(`suiteCardVerdict-${cp.index}`);
                const colour = {
                    PASS: 'var(--accent-green)', FAIL: 'var(--accent-red)',
                    UNVERIFIED: '#f59e0b', CRASH: '#f87171'
                }[cp.status] || 'var(--text-muted)';
                if (statusEl) {
                    statusEl.innerText = `Status: ${cp.status} (last run, ${cp.duration || 0}s)`;
                    statusEl.style.color = colour;
                }
                if (verdictEl && reason) {
                    verdictEl.innerHTML = `⚖️ ${escapeHtml(reason)}`;
                    verdictEl.style.color = colour;
                }
            });
            renderVerdictNotes(notes);

            const errSection = document.getElementById('errorSection');
            const errList = document.getElementById('errorLogList');
            errList.innerHTML = '';

            const consoleErrors = latestReport.console_errors || [];
            const networkErrors = latestReport.network_errors || [];
            const totalErrors = consoleErrors.length + networkErrors.length;
            if (totalErrors > 0) {
                errSection.style.display = 'flex';
                consoleErrors.forEach(err => errList.innerHTML += `<div>[Console] ${err}</div>`);
                networkErrors.forEach(err => errList.innerHTML += `<div>[Network] ${err}</div>`);
            } else {
                errSection.style.display = 'none';
            }

            // Render checkpoints thumbnails with play overlays
            const screenshotGrid = document.getElementById('screenshotGrid');
            screenshotGrid.innerHTML = '';

            const checkpoints = latestReport.checkpoints || [];
            checkpoints.forEach(cp => {
                const thumb = document.createElement('div');
                thumb.className = 'screenshot-card';
                thumb.style.position = 'relative';

                let videoOverlay = '';
                if (cp.video) {
                    videoOverlay = `
                        <div class="video-play-icon" onclick="event.stopPropagation(); openVideoModal('${cp.video}')" 
                             style="position: absolute; top: 8px; right: 8px; background: #10b981; color: white; border-radius: 50%; width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; font-size: 0.8rem; cursor: pointer; z-index: 10;" 
                             title="Watch Video Run">
                            ▶
                        </div>
                    `;
                }

                thumb.onclick = () => { if (cp.screenshot) openLightbox(cp.screenshot); };
                thumb.innerHTML = `
                    ${videoOverlay}
                    <img src="${cp.screenshot || NO_SHOT_PLACEHOLDER}" onerror="this.src=NO_SHOT_PLACEHOLDER" alt="Case Checkpoint">
                    <div class="screenshot-label">Case #${cp.index} (${cp.status})</div>
                `;
                screenshotGrid.appendChild(thumb);
            });

            // Restore AI Recommendations list
            const recList = document.getElementById('recommendationList');
            recList.innerHTML = '';
            const recs = flowSuggestions.recommendations || [
                "Implement input length boundaries to prevent layout displacement.",
                "Inject duplicate check validations on employee IDs.",
                "Ensure SSL redirects are enforced on login page."
            ];
            recs.forEach(rec => recList.innerHTML += `<li>${rec}</li>`);

            document.getElementById('reportCard').style.display = 'flex';
        }

        // Console logger
        function logConsole(message, isError = false) {
            const el = document.getElementById('recorderConsole');
            el.style.color = isError ? 'var(--accent-red)' : '#10b981';
            el.innerText = `> ${message}`;
        }

        // Terminal logger
        function logTerminal(message, color = '#a7f3d0') {
            const consoleEl = document.getElementById('terminalConsole');
            const log = document.createElement('div');
            log.className = 'terminal-log';
            log.style.color = color;
            log.innerText = message;
            consoleEl.appendChild(log);
            consoleEl.scrollTop = consoleEl.scrollHeight;
        }

        // Lightbox modal helpers
        function openLightbox(src) {
            const modal = document.getElementById('lightbox');
            const img = document.getElementById('lightboxImg');
            img.src = src;
            modal.classList.add('open');
        }

        function closeLightbox() {
            const modal = document.getElementById('lightbox');
            modal.classList.remove('open');
        }

        // Video modal helpers
        function openVideoModal(src) {
            const modal = document.getElementById('videoModal');
            const video = document.getElementById('flowVideoPlayer');
            video.src = src;
            modal.classList.add('open');
            video.play();
        }

        function closeVideoModal() {
            const modal = document.getElementById('videoModal');
            const video = document.getElementById('flowVideoPlayer');
            video.pause();
            video.src = '';
            modal.classList.remove('open');
        }

        // Upload custom test suite json file
        async function uploadCustomSuite() {
            const fileInput = document.getElementById('customSuiteUploadInput');
            const file = fileInput.files[0];
            if (!file) return;

            document.getElementById('uploadFileNameLabel').innerText = file.name;

            const formData = new FormData();
            formData.append("file", file);

            try {
                const response = await fetch(`/upload-test-cases?flow_name=${encodeURIComponent(currentFlowName)}&project_name=${encodeURIComponent(currentProjectName)}`, {
                    method: 'POST',
                    body: formData
                });

                if (!response.ok) {
                    const errData = await response.json();
                    throw new Error(errData.detail || "Upload failed");
                }

                const data = await response.json();
                alert("Success: Custom test suite loaded successfully!");
                await loadSelectedFlow();
            } catch (e) {
                alert(`Upload error: ${e.message}`);
                document.getElementById('uploadFileNameLabel').innerText = '';
            }
        }

        // ============================================================================
        // 🤖 MULTI-AGENT TEST GENERATION
        // ============================================================================

        function triggerMultiAgentGeneration() {
            const btn = document.getElementById('multiAgentBtn');
            const panel = document.getElementById('multiAgentPanel');
            const logConsole = document.getElementById('maLogConsole');

            btn.disabled = true;
            btn.innerText = '🤖 Generating...';
            panel.style.display = 'block';
            logConsole.innerHTML = '';

            // Reset agent cards
            [1, 2, 3].forEach(i => {
                document.getElementById(`maAgent${i}Status`).innerText = '⏳ Waiting...';
                document.getElementById(`maAgent${i}Status`).style.color = 'var(--text-muted)';
                if (i <= 2) document.getElementById(`maAgent${i}Count`).innerText = '0';
                else document.getElementById(`maAgent${i}Count`).innerText = '—';
                document.getElementById(`maAgent${i}Card`).style.borderColor = 'var(--border-color)';
            });
            document.getElementById('maCoveragePct').innerText = '0%';
            document.getElementById('maCoverageBar').style.width = '0%';
            document.getElementById('maTotalCases').innerText = '0';
            document.getElementById('maCoverageStatus').innerText = '';
            document.getElementById('maRoundBadge').innerText = 'Round 0/3';

            function maLog(msg, color) {
                const line = document.createElement('div');
                line.style.color = color || '#94a3b8';
                line.innerText = `[${new Date().toLocaleTimeString()}] ${msg}`;
                logConsole.appendChild(line);
                logConsole.scrollTop = logConsole.scrollHeight;
            }

            // Gather selected checkboxes for test types to guide multi-agent generation
            const checkboxes = document.querySelectorAll('input[name="testType"]:checked');
            const types = Array.from(checkboxes).map(cb => cb.value).join(",");

            const streamUrl = `/generate-multi-agent-stream?flow_name=${encodeURIComponent(currentFlowName)}&project_name=${encodeURIComponent(currentProjectName)}&test_types=${encodeURIComponent(types)}&max_iterations=3`;
            const eventSource = new EventSource(streamUrl);

            let agent1Total = 0;
            let agent2Total = 0;

            eventSource.onmessage = function (event) {
                const data = JSON.parse(event.data);

                if (data.event === 'start') {
                    maLog(data.message, '#a78bfa');
                } else if (data.event === 'round_start') {
                    document.getElementById('maRoundBadge').innerText = `Round ${data.round}/${data.max_rounds}`;
                    maLog(data.message, '#c4b5fd');
                    // Reset agent statuses for new round
                    [1, 2, 3].forEach(i => {
                        document.getElementById(`maAgent${i}Status`).innerText = '⏳ Waiting...';
                        document.getElementById(`maAgent${i}Status`).style.color = 'var(--text-muted)';
                        document.getElementById(`maAgent${i}Card`).style.borderColor = 'var(--border-color)';
                    });
                } else if (data.event === 'agent_start') {
                    const card = document.getElementById(`maAgent${data.agent}Card`);
                    const status = document.getElementById(`maAgent${data.agent}Status`);
                    card.style.borderColor = data.agent === 1 ? '#3b82f6' : data.agent === 2 ? '#ef4444' : '#f59e0b';
                    status.innerText = '🔄 Generating...';
                    status.style.color = data.agent === 1 ? '#60a5fa' : data.agent === 2 ? '#f87171' : '#fbbf24';
                    maLog(data.message, data.agent === 1 ? '#60a5fa' : data.agent === 2 ? '#f87171' : '#fbbf24');
                } else if (data.event === 'agent_done') {
                    const status = document.getElementById(`maAgent${data.agent}Status`);
                    const count = document.getElementById(`maAgent${data.agent}Count`);
                    status.innerText = '✅ Complete';
                    status.style.color = '#10b981';
                    if (data.agent === 1) { agent1Total += (data.unique_added !== undefined ? data.unique_added : data.count); count.innerText = agent1Total; }
                    if (data.agent === 2) { agent2Total += (data.unique_added !== undefined ? data.unique_added : data.count); count.innerText = agent2Total; }
                    document.getElementById(`maAgent${data.agent}Card`).style.borderColor = '#10b981';
                    maLog(data.message, '#10b981');
                } else if (data.event === 'agent_error') {
                    const status = document.getElementById(`maAgent${data.agent}Status`);
                    status.innerText = '⚠️ Error';
                    status.style.color = '#ef4444';
                    document.getElementById(`maAgent${data.agent}Card`).style.borderColor = '#ef4444';
                    maLog(data.message, '#ef4444');
                } else if (data.event === 'merge') {
                    document.getElementById('maTotalCases').innerText = data.total;
                    maLog(data.message, '#94a3b8');
                } else if (data.event === 'review_done') {
                    const pct = data.coverage_percentage || 0;
                    document.getElementById('maCoveragePct').innerText = `${pct}%`;
                    document.getElementById('maCoverageBar').style.width = `${pct}%`;
                    document.getElementById('maAgent3Count').innerText = `${pct}%`;
                    document.getElementById('maAgent3Status').innerText = data.coverage_complete ? '✅ Full Coverage' : `⚠️ ${data.missing_count} gaps`;
                    document.getElementById('maAgent3Status').style.color = data.coverage_complete ? '#10b981' : '#f59e0b';
                    document.getElementById('maAgent3Card').style.borderColor = data.coverage_complete ? '#10b981' : '#f59e0b';
                    document.getElementById('maCoverageStatus').innerText = data.coverage_complete ? '🎉 All scenarios covered!' : `${data.missing_count} missing scenario(s)`;
                    document.getElementById('maCoverageStatus').style.color = data.coverage_complete ? '#10b981' : '#f59e0b';

                    if (data.review_notes) maLog(`📝 ${data.review_notes}`, '#94a3b8');
                    maLog(data.message, '#fbbf24');

                    // Show missing items
                    if (data.missing_items && data.missing_items.length > 0) {
                        data.missing_items.forEach(m => {
                            maLog(`   ❌ Missing: ${m.matrix_key} — ${m.description}`, '#f87171');
                        });
                    }
                } else if (data.event === 'saved') {
                    maLog(data.message, '#10b981');
                    document.getElementById('maTotalCases').innerText = data.total_cases;
                } else if (data.event === 'log') {
                    maLog(data.message, '#94a3b8');
                } else if (data.event === 'error') {
                    maLog(data.message, '#ef4444');
                } else if (data.event === 'complete') {
                    maLog(data.message, '#a78bfa');
                    eventSource.close();
                    btn.disabled = false;
                    btn.innerText = '🤖 Multi-Agent Generate';
                    document.getElementById('maRoundBadge').innerText = `✅ Done (${data.rounds_executed} rounds)`;

                    // Reload the flow to show new test cases in the suite list
                    loadSelectedFlow();
                }
            };

            eventSource.onerror = function () {
                maLog('⚠️ Connection error or stream ended.', '#ef4444');
                eventSource.close();
                btn.disabled = false;
                btn.innerText = '🤖 Multi-Agent Generate';
            };
        }

        // ══════════════════════════════════════════════════════════════
        // Platform additions: preflight, editor, history, settings, exports
        // ══════════════════════════════════════════════════════════════

        // ── Generic modal helper ──────────────────────────────────────
        function openModal(title, bodyHtml, footerHtml = '') {
            document.getElementById('genericModalTitle').innerHTML = title;
            document.getElementById('genericModalBody').innerHTML = bodyHtml;
            document.getElementById('genericModalFooter').innerHTML = footerHtml ||
                `<button class="btn" style="background:#334155;" onclick="closeModal()">Close</button>`;
            document.getElementById('genericModal').style.display = 'flex';
        }

        function closeModal() {
            document.getElementById('genericModal').style.display = 'none';
        }

        function flowQuery() {
            return `flow_name=${encodeURIComponent(currentFlowName)}` +
                   `&project_name=${encodeURIComponent(currentProjectName)}`;
        }

        // ── Selector pre-flight validation ────────────────────────────
        function triggerPreflight() {
            if (!currentFlowName) return alert("Select a business flow first.");
            const terminal = document.getElementById('terminalConsole');
            terminal.innerHTML = '';
            terminal.style.display = 'block';
            logTerminal("🔍 Selector pre-flight starting...", '#a78bfa');

            const applyFix = confirm(
                "Attempt to auto-repair broken selectors with AI?\n\n" +
                "OK = validate and let the model map broken selectors onto real elements " +
                "(needs GEMINI_API_KEY; only high-confidence repairs are saved).\n" +
                "Cancel = validate only, change nothing."
            );
            const url = `/preflight-selectors-stream?${flowQuery()}&apply_fix=${applyFix}`;
            const source = new EventSource(url);
            source.onmessage = function (event) {
                const data = JSON.parse(event.data);
                if (data.event === 'progress') {
                    if (!data.exists) logTerminal(`   ❌ missing: ${data.selector}`, '#f87171');
                } else if (data.event === 'summary') {
                    logTerminal(data.message, data.missing ? '#f59e0b' : '#10b981');
                    (data.missing_items || []).forEach(item => {
                        logTerminal(`   ❌ ${item.selector} — used by: ${(item.used_by || []).join('; ')}`, '#f87171');
                    });
                } else if (data.event === 'repair') {
                    logTerminal(data.message, '#10b981');
                } else if (data.event === 'complete') {
                    logTerminal(data.message, '#a78bfa');
                    source.close();
                    if (applyFix) loadSelectedFlow();
                } else if (data.event === 'error') {
                    logTerminal(`⚠️ ${data.message}`, '#ef4444');
                    source.close();
                } else if (data.message) {
                    logTerminal(data.message);
                }
            };
            source.onerror = function () { source.close(); };
        }

        // ── Scenario editor ───────────────────────────────────────────
        function openCaseEditor(number) {
            const isNew = !number;
            const scenario = isNew
                ? { scenario_name: "New scenario", type: "positive", steps: [
                        { action: "CLICK", selector: "#someButton" },
                        { action: "VERIFY_MESSAGE", value: "expected message" }
                    ], expected_result: "" }
                : loadedTestCases[number - 1];
            if (!scenario) return;

            const body = `
                <div style="font-size:0.78rem; color:var(--text-muted); margin-bottom:0.5rem;">
                    Edit the scenario JSON. Supported actions: NAVIGATE, CLICK, TYPE, CLEAR, SELECT,
                    CHECK/UNCHECK, KEYPRESS, UPLOAD, HOVER, WAIT, DIALOG (accept/dismiss), and assertions
                    VERIFY_MESSAGE, VERIFY_ROW_CONTAINS, VERIFY_ROW_ABSENT, VERIFY_VALUE, VERIFY_TEXT,
                    VERIFY_DISABLED, VERIFY_ENABLED, VERIFY_READONLY, VERIFY_NON_ZERO, VERIFY_VISIBLE,
                    VERIFY_HIDDEN, VERIFY_URL, VERIFY_COUNT, VERIFY_DOWNLOAD.
                </div>
                <textarea id="caseEditorText" spellcheck="false"
                    style="width:100%; height:340px; font-family:Consolas,monospace; font-size:0.78rem;
                           background:rgba(0,0,0,0.45); color:#e2e8f0; border:1px solid var(--border-color);
                           border-radius:6px; padding:0.6rem;">${escapeHtml(JSON.stringify(scenario, null, 2))}</textarea>
                <div id="caseEditorError" style="color:#f87171; font-size:0.78rem; margin-top:0.4rem;"></div>`;

            const footer = `
                <button class="btn btn-green" onclick="saveCaseEditor(${number})">💾 Save scenario</button>
                <button class="btn" style="background:#334155;" onclick="closeModal()">Cancel</button>`;

            openModal(isNew ? "➕ Add scenario" : `✏️ Edit scenario #${number}`, body, footer);
        }

        async function saveCaseEditor(number) {
            const errorEl = document.getElementById('caseEditorError');
            let scenario;
            try {
                scenario = JSON.parse(document.getElementById('caseEditorText').value);
            } catch (e) {
                errorEl.innerText = `Invalid JSON: ${e.message}`;
                return;
            }
            if (!scenario.scenario_name || !Array.isArray(scenario.steps)) {
                errorEl.innerText = "The scenario needs a 'scenario_name' and a 'steps' array.";
                return;
            }
            const url = number
                ? `/update-test-case?${flowQuery()}&case_index=${number}`
                : `/add-test-case?${flowQuery()}`;
            try {
                const response = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(scenario)
                });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Save failed');
                closeModal();
                await loadSelectedFlow();
            } catch (e) {
                errorEl.innerText = e.message;
            }
        }

        async function deleteCase(number) {
            const scenario = loadedTestCases[number - 1] || {};
            if (!confirm(`Delete scenario #${number}?\n\n${scenario.scenario_name || ''}`)) return;
            try {
                const response = await fetch(`/delete-test-case?${flowQuery()}&case_index=${number}`,
                    { method: 'POST' });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Delete failed');
                await loadSelectedFlow();
            } catch (e) {
                alert(e.message);
            }
        }

        // ── Run history & comparison ──────────────────────────────────
        async function openRunHistory() {
            if (!currentFlowName) return alert("Select a business flow first.");
            try {
                const response = await fetch(`/get-run-history?${flowQuery()}`, { cache: "no-store" });
                const data = await response.json();
                const history = data.history || [];
                if (!history.length) {
                    return openModal("🕓 Run history", "<div>No runs recorded for this flow yet.</div>");
                }
                const rows = history.map(run => `
                    <tr>
                        <td>${escapeHtml(run.execution_date)}</td>
                        <td style="color:#10b981">${run.passed || 0}</td>
                        <td style="color:#f87171">${run.failed || 0}</td>
                        <td style="color:#fbbf24">${run.unverified || 0}</td>
                        <td>${run.crashed || 0}</td>
                        <td>${run.duration_seconds || 0}s</td>
                        <td>${escapeHtml(run.mode || '')}${run.triggered_by ? ` · ${escapeHtml(run.triggered_by)}` : ''}</td>
                        <td><button class="btn" style="background:#334155; font-size:0.68rem; padding:0.15rem 0.45rem;"
                            onclick="loadHistoricReport('${escapeHtml(run.report_file)}')">Load</button></td>
                    </tr>`).join('');

                const options = history.map(run =>
                    `<option value="${escapeHtml(run.report_file)}">${escapeHtml(run.execution_date)} ` +
                    `(${run.passed || 0}P/${run.failed || 0}F/${run.unverified || 0}U)</option>`).join('');

                openModal("🕓 Run history &amp; comparison", `
                    <div style="max-height:280px; overflow:auto;">
                        <table style="width:100%; border-collapse:collapse; font-size:0.78rem;">
                            <thead><tr style="color:var(--text-muted); text-align:left;">
                                <th>Date</th><th>Pass</th><th>Fail</th><th>Unver.</th><th>Crash</th>
                                <th>Time</th><th>Mode</th><th></th></tr></thead>
                            <tbody>${rows}</tbody>
                        </table>
                    </div>
                    <div style="margin-top:1rem; border-top:1px solid var(--border-color); padding-top:0.75rem;">
                        <div style="font-weight:600; margin-bottom:0.4rem;">Compare two runs</div>
                        <div style="display:flex; gap:0.5rem; align-items:center; flex-wrap:wrap;">
                            <select id="compareA" style="padding:0.35rem; font-size:0.75rem;">${options}</select>
                            <span>vs</span>
                            <select id="compareB" style="padding:0.35rem; font-size:0.75rem;">${options}</select>
                            <button id="compareRunsBtn" class="btn" style="background:#0ea5e9; font-size:0.72rem;"
                                onclick="runComparison()">Compare</button>
                        </div>
                        <div id="compareResult" style="margin-top:0.6rem; font-size:0.78rem;"></div>
                    </div>`);
            } catch (e) {
                alert(e.message);
            }
        }

        async function loadHistoricReport(reportFile) {
            try {
                const response = await fetch(`/get-report?${flowQuery()}&report_file=${encodeURIComponent(reportFile)}`,
                    { cache: "no-store" });
                const report = await response.json();
                closeModal();
                restoreExecutionReport(report);
                document.getElementById('reportCard').scrollIntoView({ behavior: 'smooth' });
            } catch (e) {
                alert(e.message);
            }
        }

        async function runComparison() {
            const a = document.getElementById('compareA').value;
            const b = document.getElementById('compareB').value;
            const target = document.getElementById('compareResult');
            target.innerHTML = 'Comparing...';
            try {
                const response = await fetch(
                    `/compare-runs?${flowQuery()}&report_a=${encodeURIComponent(a)}&report_b=${encodeURIComponent(b)}`,
                    { cache: "no-store" });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Comparison failed');
                const block = (title, items, colour) => items.length
                    ? `<div style="margin-top:0.4rem;"><b style="color:${colour}">${title} (${items.length})</b>` +
                      items.map(i => `<div style="color:var(--text-muted)">• ${escapeHtml(i.name)} ` +
                          `${i.from ? `(${i.from} → ${i.to})` : `(${i.status})`}` +
                          `${i.reason ? ` — ${escapeHtml(i.reason)}` : ''}</div>`).join('') + `</div>`
                    : '';
                target.innerHTML = `
                    <div>${escapeHtml(data.report_a.date)} → ${escapeHtml(data.report_b.date)}</div>
                    ${block('🔴 Newly failing', data.newly_failing, '#f87171')}
                    ${block('🟢 Newly passing', data.newly_passing, '#10b981')}
                    ${block('🆕 New scenarios', data.new_scenarios, '#60a5fa')}
                    ${block('🗑 Removed scenarios', data.removed_scenarios, '#94a3b8')}
                    <div style="margin-top:0.4rem; color:var(--text-muted)">Unchanged: ${data.unchanged_count}</div>`;
            } catch (e) {
                target.innerHTML = `<span style="color:#f87171">${e.message}</span>`;
            }
        }

        // ── Exports ───────────────────────────────────────────────────
        function openPrintableReport() {
            if (!currentFlowName) return;
            window.open(`/export-report-html?${flowQuery()}`, '_blank');
        }
        function downloadReportCsv() {
            if (!currentFlowName) return;
            window.location = `/export-report-csv?${flowQuery()}`;
        }
        function downloadJUnit() {
            if (!currentFlowName) return;
            window.open(`/export-junit?${flowQuery()}`, '_blank');
        }
        function downloadEvidenceBundle() {
            if (!currentFlowName) return;
            window.location = `/download-evidence?${flowQuery()}`;
        }

        // ── Environments / credentials / notifications ─────────────────
        let projectConfigCache = {};

        async function loadEnvironmentOptions() {
            const select = document.getElementById('envSelect');
            if (!select) return;
            try {
                const response = await fetch(
                    `/get-project-config?project_name=${encodeURIComponent(currentProjectName)}`,
                    { cache: "no-store" });
                projectConfigCache = await response.json();
                const active = projectConfigCache.active_env || '';
                select.innerHTML = '<option value="">Recorded URL (default)</option>';
                Object.entries(projectConfigCache.environments || {}).forEach(([name, cfg]) => {
                    const option = document.createElement('option');
                    option.value = name;
                    option.innerText = `${name} — ${cfg.base_url || ''}`;
                    if (name === active) option.selected = true;
                    select.appendChild(option);
                });
            } catch (e) { /* settings are optional */ }
        }

        async function loadApiSeedOptions() {
            const select = document.getElementById('apiSeedSelect');
            if (!select) return;
            try {
                const response = await fetch(
                    `/list-api-seeds?project_name=${encodeURIComponent(currentProjectName)}`,
                    { cache: "no-store" });
                const data = await response.json();
                select.innerHTML = '<option value="">None</option>';
                (data.seeds || []).forEach(name => {
                    const option = document.createElement('option');
                    option.value = name;
                    option.innerText = name;
                    select.appendChild(option);
                });
            } catch (e) { /* seeds are optional */ }
        }

        async function openProjectSettings() {
            try {
                const response = await fetch(
                    `/get-project-config?project_name=${encodeURIComponent(currentProjectName)}`,
                    { cache: "no-store" });
                const cfg = await response.json();
                const envJson = JSON.stringify(cfg.environments || {
                    dev: { base_url: "https://dev.example.com" },
                    staging: { base_url: "https://staging.example.com" }
                }, null, 2);

                openModal(`⚙️ Settings — ${escapeHtml(currentProjectName)}`, `
                    <div class="form-row">
                        <label><b>Environments</b> (base_url replaces the host of every recorded URL)</label>
                        <textarea id="settingsEnvJson" spellcheck="false"
                            style="width:100%; height:120px; font-family:Consolas,monospace; font-size:0.78rem;
                                   background:rgba(0,0,0,0.45); color:#e2e8f0; border:1px solid var(--border-color);
                                   border-radius:6px; padding:0.5rem;">${escapeHtml(envJson)}</textarea>
                    </div>
                    <div class="form-row" style="margin-top:0.6rem;">
                        <label><b>Active environment</b></label>
                        <input id="settingsActiveEnv" value="${escapeHtml(cfg.active_env || '')}"
                            placeholder="leave blank to use recorded URLs"
                            style="padding:0.45rem; background:rgba(0,0,0,0.35); color:white;
                                   border:1px solid var(--border-color); border-radius:4px;">
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.6rem; margin-top:0.6rem;">
                        <div class="form-row">
                            <label><b>Login username</b></label>
                            <input id="settingsUser" value="${escapeHtml((cfg.credentials || {}).username || '')}"
                                style="padding:0.45rem; background:rgba(0,0,0,0.35); color:white;
                                       border:1px solid var(--border-color); border-radius:4px;">
                        </div>
                        <div class="form-row">
                            <label><b>Login password</b></label>
                            <input id="settingsPass" type="password"
                                value="${escapeHtml((cfg.credentials || {}).password || '')}"
                                placeholder="leave ******** to keep unchanged"
                                style="padding:0.45rem; background:rgba(0,0,0,0.35); color:white;
                                       border:1px solid var(--border-color); border-radius:4px;">
                        </div>
                    </div>
                    <div class="form-row" style="margin-top:0.6rem;">
                        <label><b>Notification webhook</b> (Slack/Teams compatible; used by scheduled &amp; CI runs)</label>
                        <input id="settingsWebhook" value="${escapeHtml((cfg.notifications || {}).webhook_url || '')}"
                            placeholder="https://hooks.slack.com/services/..."
                            style="padding:0.45rem; background:rgba(0,0,0,0.35); color:white;
                                   border:1px solid var(--border-color); border-radius:4px;">
                        <select id="settingsWebhookWhen" style="padding:0.4rem; margin-top:0.35rem;">
                            <option value="always" ${((cfg.notifications || {}).on || 'always') === 'always' ? 'selected' : ''}>Notify on every run</option>
                            <option value="failures_only" ${((cfg.notifications || {}).on) === 'failures_only' ? 'selected' : ''}>Notify only on failures</option>
                        </select>
                    </div>
                    <div id="settingsError" style="color:#f87171; font-size:0.78rem; margin-top:0.4rem;"></div>`,
                    `<button class="btn btn-green" onclick="saveProjectSettings()">💾 Save settings</button>
                     <button class="btn" style="background:#334155;" onclick="closeModal()">Cancel</button>`);
            } catch (e) {
                alert(e.message);
            }
        }

        async function saveProjectSettings() {
            const errorEl = document.getElementById('settingsError');
            let environments;
            try {
                environments = JSON.parse(document.getElementById('settingsEnvJson').value || '{}');
            } catch (e) {
                errorEl.innerText = `Environments JSON invalid: ${e.message}`;
                return;
            }
            const payload = {
                environments,
                active_env: document.getElementById('settingsActiveEnv').value.trim(),
                credentials: {
                    username: document.getElementById('settingsUser').value,
                    password: document.getElementById('settingsPass').value
                },
                notifications: {
                    webhook_url: document.getElementById('settingsWebhook').value.trim(),
                    on: document.getElementById('settingsWebhookWhen').value
                }
            };
            try {
                const response = await fetch(
                    `/save-project-config?project_name=${encodeURIComponent(currentProjectName)}`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(payload) });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Save failed');
                closeModal();
                await loadEnvironmentOptions();
                alert('Project settings saved.');
            } catch (e) {
                errorEl.innerText = e.message;
            }
        }

        // ── Schedules ─────────────────────────────────────────────────
        async function openScheduleManager() {
            try {
                const response = await fetch(
                    `/list-schedules?project_name=${encodeURIComponent(currentProjectName)}`,
                    { cache: "no-store" });
                const data = await response.json();
                const rows = (data.schedules || []).map(s => `
                    <tr>
                        <td>${escapeHtml(s.name)}</td>
                        <td>${escapeHtml(s.flow_name)}</td>
                        <td>${escapeHtml(JSON.stringify(s.trigger))}</td>
                        <td>${s.enabled ? '🟢 on' : '⚪ off'}</td>
                        <td>${escapeHtml(s.last_run || '—')} ${escapeHtml(s.last_status || '')}</td>
                        <td><button class="btn btn-red" style="font-size:0.68rem; padding:0.15rem 0.45rem;"
                            onclick="removeSchedule('${escapeHtml(s.id)}')">🗑</button></td>
                    </tr>`).join('');

                openModal("⏰ Scheduled runs", `
                    <div style="font-size:0.78rem; color:var(--text-muted); margin-bottom:0.5rem;">
                        Scheduler ${data.scheduler_running ? 'is running' : 'is stopped'} inside the server process.
                        Results are sent to the notification webhook configured in project settings.
                    </div>
                    <table style="width:100%; border-collapse:collapse; font-size:0.78rem;">
                        <thead><tr style="color:var(--text-muted); text-align:left;">
                            <th>Name</th><th>Flow</th><th>Trigger</th><th>State</th><th>Last run</th><th></th>
                        </tr></thead><tbody>${rows || '<tr><td colspan="6">No schedules yet.</td></tr>'}</tbody>
                    </table>
                    <div style="margin-top:1rem; border-top:1px solid var(--border-color); padding-top:0.75rem;">
                        <div style="font-weight:600; margin-bottom:0.4rem;">Add a schedule</div>
                        <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.5rem;">
                            <input id="schedName" placeholder="Nightly regression"
                                style="padding:0.4rem; background:rgba(0,0,0,0.35); color:white; border:1px solid var(--border-color); border-radius:4px;">
                            <input id="schedFlow" value="${escapeHtml(currentFlowName || '')}" placeholder="flow name"
                                style="padding:0.4rem; background:rgba(0,0,0,0.35); color:white; border:1px solid var(--border-color); border-radius:4px;">
                            <select id="schedType" style="padding:0.4rem;">
                                <option value="daily">Daily at…</option>
                                <option value="weekly">Weekly (Mon) at…</option>
                                <option value="interval">Every N minutes</option>
                            </select>
                            <input id="schedAt" value="02:00" placeholder="02:00 or 60"
                                style="padding:0.4rem; background:rgba(0,0,0,0.35); color:white; border:1px solid var(--border-color); border-radius:4px;">
                        </div>
                        <button class="btn btn-green" style="margin-top:0.5rem; font-size:0.75rem;"
                            onclick="addSchedule()">➕ Add schedule</button>
                        <div id="schedError" style="color:#f87171; font-size:0.78rem; margin-top:0.4rem;"></div>
                    </div>`);
            } catch (e) {
                alert(e.message);
            }
        }

        async function addSchedule() {
            const type = document.getElementById('schedType').value;
            const at = document.getElementById('schedAt').value.trim();
            const trigger = type === 'interval'
                ? { type: 'interval', minutes: parseInt(at, 10) || 60 }
                : (type === 'weekly' ? { type: 'weekly', day: 'mon', at } : { type: 'daily', at });
            const payload = {
                name: document.getElementById('schedName').value.trim() || 'Scheduled run',
                flow_name: document.getElementById('schedFlow').value.trim(),
                enabled: true,
                trigger,
                config: {
                    mode: 'headless',
                    workers: parseInt(document.getElementById('workersSelect')?.value || '2', 10),
                    verdict_mode: document.getElementById('verdictModeSelect')?.value || 'rules',
                    dependencies: document.getElementById('profileDependencies')?.value || ''
                }
            };
            try {
                const response = await fetch(
                    `/save-schedule?project_name=${encodeURIComponent(currentProjectName)}`,
                    { method: 'POST', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify(payload) });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Save failed');
                openScheduleManager();
            } catch (e) {
                document.getElementById('schedError').innerText = e.message;
            }
        }

        async function removeSchedule(id) {
            if (!confirm('Remove this schedule?')) return;
            await fetch(`/delete-schedule?project_name=${encodeURIComponent(currentProjectName)}` +
                `&schedule_id=${encodeURIComponent(id)}`, { method: 'POST' });
            openScheduleManager();
        }

        // ── Flaky detection ───────────────────────────────────────────
        async function openFlakyReport() {
            try {
                const response = await fetch(
                    `/flaky-tests?project_name=${encodeURIComponent(currentProjectName)}`,
                    { cache: "no-store" });
                const data = await response.json();
                const rows = (data.flaky || []).map(item => `
                    <tr><td>${escapeHtml(item.flow_name)}</td>
                        <td>${escapeHtml(item.scenario_name)}</td>
                        <td>${item.flips}</td>
                        <td style="color:var(--text-muted)">${item.recent_statuses.join(' → ')}</td></tr>`).join('');
                openModal("🎲 Flaky scenarios", `
                    <div style="font-size:0.78rem; color:var(--text-muted); margin-bottom:0.5rem;">
                        Scenarios whose result flipped between runs. Flakiness usually means a timing
                        issue or a selector that only sometimes resolves — fix these before trusting the suite.
                    </div>
                    <table style="width:100%; border-collapse:collapse; font-size:0.78rem;">
                        <thead><tr style="color:var(--text-muted); text-align:left;">
                            <th>Flow</th><th>Scenario</th><th>Flips</th><th>Recent runs</th></tr></thead>
                        <tbody>${rows || '<tr><td colspan="4">No flaky scenarios detected yet — run a suite a few times.</td></tr>'}</tbody>
                    </table>`);
            } catch (e) {
                alert(e.message);
            }
        }

        // ── Flow & project maintenance ────────────────────────────────
        async function deleteCurrentFlow() {
            if (!currentFlowName) return alert("Select a business flow first.");
            if (!confirm(`Move flow '${currentFlowName}' to .trash?\n\nRecordings, test cases and reports ` +
                         `are kept in the .trash folder so this can be undone manually.`)) return;
            try {
                const response = await fetch(`/delete-flow?${flowQuery()}`, { method: 'POST' });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Delete failed');
                alert(data.message);
                currentFlowName = "";
                await fetchSavedFlows();
                resetAIWorkspace();
            } catch (e) {
                alert(e.message);
            }
        }

        async function renameCurrentFlow() {
            if (!currentFlowName) return alert("Select a business flow first.");
            const newName = prompt("New flow name:", currentFlowName);
            if (!newName || newName === currentFlowName) return;
            try {
                const response = await fetch(`/rename-flow?${flowQuery()}&new_name=${encodeURIComponent(newName)}`,
                    { method: 'POST' });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Rename failed');
                await fetchSavedFlows(newName);
                await loadSelectedFlow();
            } catch (e) {
                alert(e.message);
            }
        }

        async function duplicateCurrentFlow() {
            if (!currentFlowName) return alert("Select a business flow first.");
            const newName = prompt("Name for the copy:", `${currentFlowName} copy`);
            if (!newName) return;
            try {
                const response = await fetch(`/duplicate-flow?${flowQuery()}&new_name=${encodeURIComponent(newName)}`,
                    { method: 'POST' });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Duplicate failed');
                await fetchSavedFlows(newName);
                await loadSelectedFlow();
            } catch (e) {
                alert(e.message);
            }
        }

        async function deleteCurrentProject() {
            if (!confirm(`Move the whole project '${currentProjectName}' to .trash?`)) return;
            try {
                const response = await fetch(
                    `/delete-project?project_name=${encodeURIComponent(currentProjectName)}`,
                    { method: 'POST' });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Delete failed');
                alert(data.message);
                location.reload();
            } catch (e) {
                alert(e.message);
            }
        }

        // ── Data matrix upload ────────────────────────────────────────
        async function uploadDataMatrix(input) {
            if (!input.files.length || !currentFlowName) return;
            const form = new FormData();
            form.append('file', input.files[0]);
            try {
                const response = await fetch(`/upload-data-matrix?${flowQuery()}`,
                    { method: 'POST', body: form });
                const data = await response.json();
                if (!response.ok) throw new Error(data.detail || 'Upload failed');
                alert(`${data.message}\nColumns available as {{variables}}: ${(data.columns || []).join(', ')}`);
                await loadSelectedFlow();
            } catch (e) {
                alert(e.message);
            } finally {
                input.value = '';
            }
        }

        // ── Platform status / API key ─────────────────────────────────
        async function refreshPlatformStatus() {
            try {
                const response = await fetch('/platform-info', { cache: "no-store" });
                if (response.status === 401) {
                    promptForApiKey();
                    return;
                }
                const info = await response.json();
                const el = document.getElementById('platformStatus');
                if (el) {
                    el.innerHTML =
                        `<span title="Workspace folder">📁 ${escapeHtml(info.workspace)}</span> · ` +
                        `<span title="Gemini model">${info.ai_configured ? '🟢 AI ready' : '🔴 no API key'} (${escapeHtml(info.model)})</span> · ` +
                        `<span title="Scheduler">${info.scheduler ? '⏰ scheduler on' : '⏰ scheduler off'}</span> · ` +
                        `<span title="Approximate tokens used across recent AI calls">🧮 ~${(info.llm_usage.approx_prompt_tokens + info.llm_usage.approx_response_tokens).toLocaleString()} tokens</span>`;
                }
            } catch (e) { /* status panel is best effort */ }
        }

        function promptForApiKey() {
            const key = prompt("This server requires an API key (QA_API_KEY). Enter it to continue:");
            if (key) {
                document.cookie = `qa_api_key=${encodeURIComponent(key)}; path=/; SameSite=Strict`;
                location.reload();
            }
        }

        // Initial setup on page load
        fetchProjects();
        refreshPlatformStatus();
