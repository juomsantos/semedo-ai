/**
 * dashboard.js — Real-time dashboard logic with 1.5 second polling
 */

const POLL_INTERVAL = 2000; // 1.5 seconds

let pollTimer = null;
let lastUpdate = new Date();
let completedTasksCache = [];        // populated on first focus of context search
let selectedContextFiles = [];       // array of {task_id, output_path, description_preview}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    setupEventListeners();
    startPolling();
    updateLogs('orchestrator');
});

// Setup event listeners
function setupEventListeners() {
    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            switchTab(e.target.dataset.tab);
        });
    });

    // History filter
    document.getElementById('history-filter').addEventListener('change', updateHistoryTasks);

    // Log agent selection
    document.getElementById('log-agent-select').addEventListener('change', (e) => {
        updateLogs(e.target.value);
    });

    // Submit task form
    const submitForm = document.getElementById('submit-task-form');
    if (submitForm) {
        submitForm.addEventListener('submit', submitTask);
    }

    // Clear cache button
    const clearCacheBtn = document.getElementById('clear-cache-btn');
    if (clearCacheBtn) {
        clearCacheBtn.addEventListener('click', handleClearCache);
    }

    // Modal close
    document.querySelector('.modal-close').addEventListener('click', closeModal);
    document.getElementById('task-modal').addEventListener('click', (e) => {
        if (e.target.id === 'task-modal') closeModal();
    });
    document.getElementById('payload-modal').addEventListener('click', (e) => {
        if (e.target.id === 'payload-modal') closePayloadModal();
    });

    // Task list event delegation
    setupTaskListDelegation();

    // Context files picker
    const contextSearch = document.getElementById('context-search');
    if (contextSearch) {
        contextSearch.addEventListener('focus', async () => {
            await loadCompletedTasks();
            renderContextDropdown(contextSearch.value);
        });
        contextSearch.addEventListener('input', () => {
            renderContextDropdown(contextSearch.value);
        });
        contextSearch.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                document.getElementById('context-dropdown').style.display = 'none';
            }
        });
        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.context-picker')) {
                document.getElementById('context-dropdown').style.display = 'none';
            }
        });
    }
}

// Setup event delegation for task lists (handles both active and history tabs)
function setupTaskListDelegation() {
    // Active tasks container
    const activeTasks = document.getElementById('active-tasks');
    if (activeTasks) {
        activeTasks.addEventListener('click', handleTaskListClick);
    }

    // History tasks container
    const historyTasks = document.getElementById('history-tasks');
    if (historyTasks) {
        historyTasks.addEventListener('click', handleTaskListClick);
    }
}

// Handle clicks in task lists (show detail or expand/collapse)
function handleTaskListClick(e) {
    const expandBtn = e.target.closest('.expand-toggle');
    if (expandBtn) {
        e.stopPropagation();
        toggleTaskExpansion(expandBtn);
        return;
    }

    const taskItem = e.target.closest('.task-item');
    if (taskItem) {
        showTaskDetail(taskItem.dataset.taskId);
    }
}

// Start real-time polling
function startPolling() {
    console.log('Starting dashboard polling (' + POLL_INTERVAL + 'ms interval)');
    
    // Initial update
    updateDashboard();
    
    // Poll every 1.5 seconds
    pollTimer = setInterval(updateDashboard, POLL_INTERVAL);
}

function stopPolling() {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
}

// Main update function
async function updateDashboard() {
    try {
        // Update status metrics
        await updateStatus();
        
        // Update active tasks (processing)
        await updateActiveTasks();
        
        // Update approvals (always, for badge)
        await updateApprovals();
        
        // Update history if visible
        if (document.getElementById('tab-history').classList.contains('active')) {
            await updateHistoryTasks();
        }
        
        // Update agent stats if visible
        if (document.getElementById('tab-agents').classList.contains('active')) {
            await updateAgentStats();
        }

        // Update logs if visible
        if (document.getElementById('tab-logs').classList.contains('active')) {
            const selectedAgent = document.getElementById('log-agent-select').value || 'orchestrator';
            await updateLogs(selectedAgent);
        }

        // Update timestamp
        lastUpdate = new Date();
        updateLastUpdateTime();
        setPollStatus(true);
    } catch (error) {
        console.error('Dashboard update error:', error);
        setPollStatus(false);
    }
}

// Update system status metrics
async function updateStatus() {
    try {
        const response = await fetch('/api/status');
        const data = await response.json();
        
        document.getElementById('metric-pending').textContent = data.counts.pending;
        document.getElementById('metric-processing').textContent = data.counts.processing;
        document.getElementById('metric-completed').textContent = data.counts.completed;
        document.getElementById('metric-failed').textContent = data.counts.failed;
        document.getElementById('metric-approval').textContent = data.counts.awaiting_approval;
    } catch (error) {
        console.error('Error updating status:', error);
    }
}

// Update approvals
async function updateApprovals() {
    try {
        const response = await fetch('/api/pending-approvals');
        const data = await response.json();
        
        const container = document.getElementById('approval-tasks');
        const count = document.getElementById('approval-count');
        const badge = document.getElementById('approval-badge');
        
        count.textContent = `${data.count} ${data.count === 1 ? 'task' : 'tasks'}`;
        badge.textContent = data.count;
        badge.style.display = data.count > 0 ? 'inline' : 'none';
        
        if (data.tasks.length === 0) {
            container.innerHTML = '<p class="no-data">No tasks awaiting approval</p>';
            return;
        }
        
        container.innerHTML = data.tasks.map(task => createApprovalTaskElement(task)).join('');
    } catch (error) {
        console.error('Error updating approvals:', error);
    }
}

// Create approval task element HTML
function createApprovalTaskElement(task) {
    const ageStr = formatAge(task.age_seconds);
    
    return `
        <div class="task-item approval" data-task-id="${task.id}">
            <div class="task-header">
                <span class="task-id">${task.id}</span>
                <span class="task-status approval">${task.status}</span>
            </div>
            <div class="task-meta">
                <span class="task-type">${task.type}</span>
                <span class="task-priority priority-${task.priority}">${task.priority}</span>
                <span class="task-age">${ageStr}</span>
            </div>
            <div class="task-info">
                <span class="task-creator">${task.created_by}</span>
                <span class="task-assigned">→ ${task.assigned_to}</span>
            </div>
            <div class="task-actions">
                <button class="btn btn-small btn-success" onclick="approveTask('${task.id}')">Approve</button>
                <button class="btn btn-small btn-danger" onclick="rejectTask('${task.id}')">Reject</button>
            </div>
            <div class="task-body">
                <pre>${escapeHtml(task.body)}</pre>
            </div>
        </div>
    `;
}

// Approve task
async function approveTask(taskId) {
    try {
        const response = await fetch(`/api/pending-approvals/${taskId}/approve`, {
            method: 'POST',
        });
        
        if (response.ok) {
            await updateApprovals();
            updateStatus();
            showNotification(`Task ${taskId} approved!`, 'success');
        } else {
            const data = await response.json();
            showNotification(data.error || 'Failed to approve task', 'error');
        }
    } catch (error) {
        console.error('Error approving task:', error);
        showNotification('Error approving task', 'error');
    }
}

// Reject task
async function rejectTask(taskId) {
    const reason = window.prompt('Reason for rejection (optional):', 'Rejected by user');
    
    if (reason === null) return; // User cancelled
    
    try {
        const response = await fetch(`/api/pending-approvals/${taskId}/reject`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ reason: reason || 'Rejected by user' }),
        });
        
        if (response.ok) {
            await updateApprovals();
            updateStatus();
            showNotification(`Task ${taskId} rejected!`, 'success');
        } else {
            const data = await response.json();
            showNotification(data.error || 'Failed to reject task', 'error');
        }
    } catch (error) {
        console.error('Error rejecting task:', error);
        showNotification('Error rejecting task', 'error');
    }
}

// Update active (processing) tasks
async function updateActiveTasks() {
    try {
        const response = await fetch('/api/tasks?status=processing');
        const data = await response.json();

        const container = document.getElementById('active-tasks');
        const count = document.getElementById('active-count');

        count.textContent = `${data.count} ${data.count === 1 ? 'task' : 'tasks'}`;

        if (data.tasks.length === 0) {
            container.innerHTML = '<p class="no-data">No tasks processing</p>';
            return;
        }

        container.innerHTML = renderTaskHierarchy(data.tasks);
    } catch (error) {
        console.error('Error updating active tasks:', error);
    }
}

// Update history tasks
async function updateHistoryTasks() {
    try {
        const filter = document.getElementById('history-filter').value;
        const url = filter
            ? `/api/tasks?status=${filter}&limit=50`
            : '/api/tasks?limit=50';

        const response = await fetch(url);
        const data = await response.json();

        const container = document.getElementById('history-tasks');

        if (data.tasks.length === 0) {
            container.innerHTML = '<p class="no-data">No tasks</p>';
            return;
        }

        container.innerHTML = renderTaskHierarchy(data.tasks, filter);
    } catch (error) {
        console.error('Error updating history:', error);
    }
}

// Update agent statistics
async function updateAgentStats() {
    try {
        const response = await fetch('/api/agents');
        const stats = await response.json();

        const tbody = document.getElementById('agent-stats-body');
        const rows = Object.entries(stats).map(([agent, data]) => {
            const promptTokens = data.prompt_tokens || 0;
            const completionTokens = data.completion_tokens || 0;
            const llmCalls = data.llm_calls || 0;

            // Show "—" for claude-code if all token values are 0
            const showDash = agent === 'claude-code' && promptTokens === 0 && completionTokens === 0 && llmCalls === 0;

            const promptDisplay = showDash ? '—' : promptTokens.toLocaleString();
            const completionDisplay = showDash ? '—' : completionTokens.toLocaleString();
            const callsDisplay = showDash ? '—' : llmCalls.toLocaleString();

            return `
                <tr>
                    <td><strong>${agent}</strong></td>
                    <td>${data.completed}</td>
                    <td class="error">${data.errors}</td>
                    <td>${promptDisplay}</td>
                    <td>${completionDisplay}</td>
                    <td>${callsDisplay}</td>
                </tr>
            `;
        }).join('');

        tbody.innerHTML = rows || '<tr><td colspan="6" class="no-data">No agents</td></tr>';
    } catch (error) {
        console.error('Error updating agent stats:', error);
    }
}

// Update logs for a specific agent
async function updateLogs(agent) {
    try {
        const response = await fetch(`/api/agents/${agent}/logs?lines=50`);
        const data = await response.json();

        const container = document.getElementById('logs-list');

        if (!data.logs || data.logs.length === 0) {
            container.innerHTML = '<p class="no-data">No logs available for ' + agent + '</p>';
            return;
        }

        container.innerHTML = [...data.logs].reverse().map(log => `
            <div class="log-entry">${escapeHtml(log)}</div>
        `).join('');

        // Latest entries are now at the top — scroll to top
        container.parentElement.scrollTop = 0;
    } catch (error) {
        console.error('Error updating logs:', error);
        const container = document.getElementById('logs-list');
        container.innerHTML = '<p class="no-data">Error loading logs: ' + error.message + '</p>';
    }
}

// Load completed tasks for context picker (lazy load)
async function loadCompletedTasks() {
    if (completedTasksCache.length > 0) return; // already loaded
    try {
        const resp = await fetch('/api/tasks/completed');
        if (resp.ok) {
            completedTasksCache = await resp.json();
        }
    } catch (e) {
        console.warn('Could not load completed tasks:', e);
    }
}

// Render context dropdown with filtered results
function renderContextDropdown(query) {
    const list = document.getElementById('context-dropdown-list');
    const dropdown = document.getElementById('context-dropdown');
    const q = query.trim().toLowerCase();
    const matches = q
        ? completedTasksCache.filter(t =>
            t.description_preview.toLowerCase().includes(q) ||
            t.task_id.toLowerCase().includes(q)
          )
        : completedTasksCache.slice(0, 20); // show 20 most recent when no query
    if (matches.length === 0) {
        list.innerHTML = '<p class="context-no-results">No matching completed tasks</p>';
    } else {
        list.innerHTML = matches.map(t => {
            const alreadySelected = selectedContextFiles.some(s => s.task_id === t.task_id);
            return `<div
                class="context-dropdown-item${alreadySelected ? ' already-selected' : ''}"
                data-task-id="${t.task_id}"
                data-output-path="${t.output_path}"
                data-preview="${t.description_preview.replace(/"/g, '&quot;')}"
                title="${t.description_preview}"
            >
                <span class="context-item-type">${t.type}</span>
                <span class="context-item-preview">${t.description_preview || t.task_id}</span>
                <span class="context-item-date">${t.created_at ? t.created_at.slice(0, 10) : ''}</span>
            </div>`;
        }).join('');
        // Click handler for each item
        list.querySelectorAll('.context-dropdown-item:not(.already-selected)').forEach(item => {
            item.addEventListener('click', () => {
                addContextFile(item.dataset.taskId, item.dataset.outputPath, item.dataset.preview);
                document.getElementById('context-search').value = '';
                document.getElementById('context-dropdown').style.display = 'none';
            });
        });
    }
    dropdown.style.display = 'block';
}

// Add a context file to the selection
function addContextFile(taskId, outputPath, preview) {
    if (selectedContextFiles.some(s => s.task_id === taskId)) return; // deduplicate
    selectedContextFiles.push({ task_id: taskId, output_path: outputPath, description_preview: preview });
    renderContextChips();
}

// Remove a context file from the selection
function removeContextFile(taskId) {
    selectedContextFiles = selectedContextFiles.filter(s => s.task_id !== taskId);
    renderContextChips();
}

// Render selected files as removable chips
function renderContextChips() {
    const container = document.getElementById('context-selected');
    if (selectedContextFiles.length === 0) {
        container.style.display = 'none';
        container.innerHTML = '';
        return;
    }
    container.style.display = 'flex';
    container.innerHTML = selectedContextFiles.map(s => `
        <div class="context-chip" title="${s.output_path}">
            <span class="chip-label">${s.description_preview ? s.description_preview.slice(0, 50) : s.task_id}</span>
            <button class="chip-remove" data-task-id="${s.task_id}" aria-label="Remove">&times;</button>
        </div>
    `).join('');
    container.querySelectorAll('.chip-remove').forEach(btn => {
        btn.addEventListener('click', () => removeContextFile(btn.dataset.taskId));
    });
}

// Submit task form
async function submitTask(event) {
    event.preventDefault();

    const form = document.getElementById('submit-task-form');
    const submitBtn = document.getElementById('submit-btn');
    const statusDiv = document.getElementById('submit-status');
    const messageDiv = document.getElementById('submit-message');

    const description = document.getElementById('task-description').value.trim();
    const type = document.getElementById('task-type').value;
    const priority = document.getElementById('task-priority').value;
    const expectedOutput = document.getElementById('task-expected-output').value.trim();

    // Client-side validation
    if (!description) {
        showSubmitStatus('Description is required', 'error');
        return;
    }

    // Disable button and show loading state
    submitBtn.disabled = true;
    submitBtn.textContent = 'Submitting...';

    try {
        const response = await fetch('/api/tasks/submit', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                description: description,
                type: type,
                priority: priority,
                expected_output: expectedOutput,
                context_files: selectedContextFiles.map(s => s.output_path),
            }),
        });

        const data = await response.json();

        if (response.ok) {
            showSubmitStatus(`Task submitted: ${data.task_id}`, 'success');
            document.getElementById('task-description').value = '';
            document.getElementById('task-expected-output').value = '';
            // Reset context files picker
            selectedContextFiles = [];
            renderContextChips();
            completedTasksCache = []; // force a fresh fetch next time
            // Keep type and priority for quick resubmit
        } else {
            showSubmitStatus(data.error || 'Failed to submit task', 'error');
        }
    } catch (error) {
        console.error('Error submitting task:', error);
        showSubmitStatus('Network error: ' + error.message, 'error');
    } finally {
        // Re-enable button
        submitBtn.disabled = false;
        submitBtn.textContent = 'Submit Task';
    }
}

// Show submit status message with auto-hide
function showSubmitStatus(message, type) {
    const statusDiv = document.getElementById('submit-status');
    const messageDiv = document.getElementById('submit-message');

    messageDiv.textContent = message;
    statusDiv.className = `submit-status ${type}`;
    statusDiv.style.display = 'block';

    // Auto-hide after 5 seconds
    setTimeout(() => {
        statusDiv.style.display = 'none';
    }, 5000);
}

// Show a transient toast notification
function showNotification(message, type) {
    // Remove any existing notification
    const existing = document.getElementById('toast-notification');
    if (existing) existing.remove();

    const toast = document.createElement('div');
    toast.id = 'toast-notification';
    toast.className = `toast-notification ${type}`;
    toast.textContent = message;
    document.body.appendChild(toast);

    // Trigger animation then auto-remove
    setTimeout(() => toast.classList.add('show'), 10);
    setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// Build task hierarchy from flat list
function buildTaskHierarchy(tasks, filter) {
    const taskMap = {};
    const roots = [];

    let filteredTasks = (filter === 'failed') ? tasks : tasks.filter(fl => !(fl.location && fl.location === 'failed'))
    // Index all tasks by ID
    filteredTasks.forEach(task => {
        taskMap[task.id] = { ...task, subtasks: [] };
    });

    // Organize into parent-child relationships
    filteredTasks.forEach(task => {
        if (task.parent_task_id && taskMap[task.parent_task_id]) {
            taskMap[task.parent_task_id].subtasks.push(taskMap[task.id]);
        } else {
            roots.push(taskMap[task.id]);
        }
    });

    return roots;
}

// Render task hierarchy as HTML
function renderTaskHierarchy(tasks, filter) {
    const roots = buildTaskHierarchy(tasks, filter);
    return roots.map(task => renderTaskWithChildren(task)).join('');
}

// Render a task and its subtasks (always render, use CSS to hide/show)
function renderTaskWithChildren(task, depth = 0) {
    const isParent = task.subtasks && task.subtasks.length > 0;
    const isExpanded = isParent && isTaskExpanded(task.id);
    const indent = depth > 0 ? `style="margin-left: ${depth * 32}px;"` : '';
    const subtasksDisplay = isParent && !isExpanded ? 'style="display: none;"' : '';

    let html = `
        <div class="task-item ${task.status.toLowerCase()}" data-task-id="${task.id}" ${indent}>
            <div class="task-header">
                ${isParent ? `<button class="expand-toggle ${isExpanded ? 'expanded' : ''}">▶</button>` : '<span class="expand-toggle-placeholder"></span>'}
                <span class="task-id">${task.id}</span>
                <span class="task-status ${task.status.toLowerCase()}">${task.status}</span>
                <span class="task-label-badge">${isParent ? 'Parent' : task.parent_task_id ? 'Subtask' : ''}</span>
            </div>
            <div class="task-meta">
                <span class="task-type">${task.type}</span>
                <span class="task-priority priority-${task.priority}">${task.priority}</span>
                <span class="task-age">${formatAge(task.age_seconds)}</span>
                ${task.iteration ? `<span class="task-iteration">Iteration ${task.iteration}</span>` : ''}
            </div>
            <div class="task-info">
                <span class="task-creator">${task.created_by}</span>
                <span class="task-assigned">→ ${task.assigned_to}</span>
                ${task.retry_count > 0 ? `<span class="task-retry">Retry ${task.retry_count}</span>` : ''}
            </div>`;

    // Add background color for subtasks
    if (task.parent_task_id) {
        html = html.replace('class="task-item', 'class="task-item subtask');
    }

    html += `</div>`;

    // Always render subtasks (they may be hidden by CSS/display)
    if (isParent) {
        const subtasksHtml = task.subtasks.map(subtask => renderTaskWithChildren(subtask, depth + 1)).join('');
        // Wrap subtasks in a container for easier hiding/showing
        html += `<div class="task-subtasks" data-parent-id="${task.id}" ${subtasksDisplay}>${subtasksHtml}</div>`;
    }

    return html;
}

// Check if task is expanded in localStorage
function isTaskExpanded(taskId) {
    return localStorage.getItem(`task_expanded_${taskId}`) === 'true';
}

// Toggle task expansion and persist to localStorage
function toggleTaskExpansion(button) {
    const taskItem = button.closest('.task-item');
    const taskId = taskItem.dataset.taskId;
    const isCurrentlyExpanded = button.classList.contains('expanded');

    if (isCurrentlyExpanded) {
        // Collapse
        button.classList.remove('expanded');
        localStorage.setItem(`task_expanded_${taskId}`, 'false');

        // Hide the subtasks container
        const subtasksContainer = taskItem.nextElementSibling;
        if (subtasksContainer && subtasksContainer.classList.contains('task-subtasks')) {
            subtasksContainer.style.display = 'none';
        }
    } else {
        // Expand
        button.classList.add('expanded');
        localStorage.setItem(`task_expanded_${taskId}`, 'true');

        // Show the subtasks container
        const subtasksContainer = taskItem.nextElementSibling;
        if (subtasksContainer && subtasksContainer.classList.contains('task-subtasks')) {
            subtasksContainer.style.display = '';
        }
    }
}

// Create task element HTML (legacy, now mostly unused)
function createTaskElement(task) {
    const statusClass = task.status.toLowerCase();
    const ageStr = formatAge(task.age_seconds);

    return `
        <div class="task-item ${statusClass}" data-task-id="${task.id}">
            <div class="task-header">
                <span class="task-id">${task.id}</span>
                <span class="task-status ${statusClass}">${task.status}</span>
            </div>
            <div class="task-meta">
                <span class="task-type">${task.type}</span>
                <span class="task-priority priority-${task.priority}">${task.priority}</span>
                <span class="task-age">${ageStr}</span>
            </div>
            <div class="task-info">
                <span class="task-creator">${task.created_by}</span>
                <span class="task-assigned">→ ${task.assigned_to}</span>
                ${task.retry_count > 0 ? `<span class="task-retry">Retry ${task.retry_count}</span>` : ''}
            </div>
        </div>
    `;
}

// Show task detail modal
async function showTaskDetail(taskId) {
    try {
        // Fetch task metadata and payload in parallel
        const [taskResponse, payloadResponse] = await Promise.all([
            fetch(`/api/tasks/${taskId}`),
            fetch(`/api/tasks/${taskId}/payload`),
        ]);
        const task = await taskResponse.json();
        const payloadData = payloadResponse.ok ? await payloadResponse.json() : null;

        // Extract body: prefer full body from API, fall back to parsing payload, then body_preview
        let taskBody = task.body || '';
        if (!taskBody && payloadData && payloadData.content) {
            // Split on the YAML frontmatter boundary (first two --- delimiters)
            const match = payloadData.content.match(/^---[\s\S]*?---\n?([\s\S]*)$/);
            if (match) {
                taskBody = match[1].trim();
            }
        }
        if (!taskBody && task.body_preview) {
            taskBody = task.body_preview;
        }

        const modal = document.getElementById('task-modal');
        const title = document.getElementById('modal-title');
        const body = document.getElementById('modal-body');

        title.textContent = `Task: ${task.id}`;

        let html = `
            <div class="task-detail">
                <div class="detail-section">
                    <h4>Metadata</h4>
                    <div class="detail-grid">
                        <div><strong>Type:</strong> ${task.type}</div>
                        <div><strong>Priority:</strong> ${task.priority}</div>
                        <div><strong>Status:</strong> ${task.status}</div>
                        <div><strong>Location:</strong> ${task.location}</div>
                        <div><strong>Created by:</strong> ${task.created_by}</div>
                        <div><strong>Assigned to:</strong> ${task.assigned_to}</div>
                        <div><strong>Created at:</strong> ${task.created_at}</div>
                        <div><strong>Age:</strong> ${formatAge(task.age_seconds)}</div>
                        ${task.retry_count > 0 ? `<div><strong>Retries:</strong> ${task.retry_count}</div>` : ''}
                    </div>
                </div>
        `;

        if (taskBody) {
            html += `
                <div class="detail-section">
                    <h4>Task Body</h4>
                    <pre class="detail-result">${escapeHtml(taskBody)}</pre>
                </div>
            `;
        }

        if (task.logs && task.logs.length > 0) {
            html += `
                <div class="detail-section">
                    <h4>Logs</h4>
                    <div class="detail-logs">
                        ${task.logs.map(log => `
                            <div class="log-entry">
                                <span class="log-timestamp">${log.timestamp}</span>
                                <span class="log-level ${log.level.toLowerCase()}">${log.level}</span>
                                <span class="log-agent">${log.agent}</span>
                                <span class="log-message">${log.message}</span>
                            </div>
                        `).join('')}
                    </div>
                </div>
            `;
        }

        if (task.result) {
            html += `
                <div class="detail-section">
                    <h4>Result</h4>
                    <pre class="detail-result">${escapeHtml(task.result)}</pre>
                </div>
            `;
        }

        html += `</div>`;
        body.innerHTML = html;
        modal.classList.add('show');
    } catch (error) {
        console.error('Error loading task detail:', error);
    }
}

// Close modal
function closeModal() {
    document.getElementById('task-modal').classList.remove('show');
}

// Show task payload in modal
async function showTaskPayload(taskId) {
    try {
        const response = await fetch(`/api/tasks/${taskId}/payload`);
        const data = await response.json();

        if (!data.content) {
            showNotification('Failed to load task payload', 'error');
            return;
        }

        const modal = document.getElementById('payload-modal');
        const title = document.getElementById('payload-modal-title');
        const body = document.getElementById('payload-modal-body');

        title.textContent = `Task Payload: ${taskId}`;
        body.innerHTML = `<pre class="payload-content">${escapeHtml(data.content)}</pre>`;
        modal.classList.add('show');
    } catch (error) {
        console.error('Error loading task payload:', error);
        showNotification('Error loading task payload', 'error');
    }
}

// Close payload modal
function closePayloadModal() {
    document.getElementById('payload-modal').classList.remove('show');
}

// Switch tabs
function switchTab(tabName) {
    // Hide all tabs
    document.querySelectorAll('.tab-content').forEach(el => {
        el.classList.remove('active');
    });
    
    // Deactivate all tab buttons
    document.querySelectorAll('.tab-btn').forEach(el => {
        el.classList.remove('active');
    });
    
    // Show selected tab
    document.getElementById(`tab-${tabName}`).classList.add('active');
    
    // Activate button
    document.querySelector(`[data-tab="${tabName}"]`).classList.add('active');
    
    // Update content if needed
    if (tabName === 'history') {
        updateHistoryTasks();
    } else if (tabName === 'agents') {
        updateAgentStats();
    } else if (tabName === 'approvals') {
        updateApprovals();
    } else if (tabName === 'logs') {
        const selectedAgent = document.getElementById('log-agent-select').value || 'orchestrator';
        updateLogs(selectedAgent);
    }
}

// Update last update timestamp
function updateLastUpdateTime() {
    const time = lastUpdate.toLocaleTimeString();
    document.getElementById('last-update').textContent = `Last updated: ${time}`;
}

// Set poll status indicator
function setPollStatus(success) {
    const status = document.getElementById('poll-status');
    if (success) {
        status.classList.remove('error');
        status.textContent = '● Polling...';
    } else {
        status.classList.add('error');
        status.textContent = '● Error';
    }
}

// Handle clear cache button click
async function handleClearCache() {
    const confirmed = confirm('Are you sure? This will delete all task files, logs, and token data.\n\nThis action cannot be undone.');
    if (!confirmed) return;

    const btn = document.getElementById('clear-cache-btn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Clearing...';
    btn.style.opacity = '0.6';

    try {
        const response = await fetch('/api/clear-cache', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });

        if (!response.ok) {
            const data = await response.json();
            throw new Error(data.message || 'Failed to clear cache');
        }

        // Auto-refresh dashboard
        setTimeout(() => {
            location.reload();
        }, 500);
    } catch (error) {
        showNotification(`Error: ${error.message}`, 'error');
        btn.disabled = false;
        btn.textContent = originalText;
        btn.style.opacity = '1';
    }
}

// Show notification/toast message
function showNotification(message, type = 'info') {
    const notification = document.createElement('div');
    const bgColor = type === 'success' ? '#4facfe' : (type === 'error' ? '#fa709a' : '#667eea');

    notification.style.cssText = `
        position: fixed;
        top: 80px;
        right: 20px;
        padding: 14px 20px;
        border-radius: 6px;
        font-weight: 600;
        font-size: 13px;
        z-index: 2000;
        animation: notificationSlideIn 0.3s ease;
        background: ${bgColor};
        color: white;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
        max-width: 300px;
    `;
    notification.textContent = message;
    document.body.appendChild(notification);

    // Add animation keyframes if not already present
    if (!document.getElementById('notification-styles')) {
        const style = document.createElement('style');
        style.id = 'notification-styles';
        style.textContent = `
            @keyframes notificationSlideIn {
                from {
                    transform: translateX(400px);
                    opacity: 0;
                }
                to {
                    transform: translateX(0);
                    opacity: 1;
                }
            }
        `;
        document.head.appendChild(style);
    }

    // Remove notification after 2.5 seconds
    setTimeout(() => {
        notification.remove();
    }, 2500);
}

// Format age in human-readable format
function formatAge(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
    return `${Math.round(seconds / 3600)}h`;
}

// Escape HTML
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}
