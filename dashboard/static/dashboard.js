/**
 * dashboard.js — Real-time dashboard logic with 1.5 second polling
 */

const POLL_INTERVAL = 2000; // 1.5 seconds

// Per-process token embedded in the served HTML as <meta name="dashboard-token">.
// Sent on every state-changing request (POST/DELETE) as the X-Dashboard-Token
// header so loopback-but-different-origin scripts can't forge approve/submit
// /clear-cache calls. When the server restarts and regenerates the token,
// open tabs need a refresh — fetch will start returning 401 until then.
const DASHBOARD_TOKEN = (
    document.querySelector('meta[name="dashboard-token"]')?.getAttribute('content') || ''
);

/**
 * Merge the X-Dashboard-Token header into a fetch init object without
 * clobbering any caller-supplied headers. Use this for every POST/DELETE.
 */
function withAuth(init = {}) {
    const headers = new Headers(init.headers || {});
    headers.set('X-Dashboard-Token', DASHBOARD_TOKEN);
    return { ...init, headers };
}

let pollTimer = null;
let lastUpdate = new Date();
let completedTasksCache = [];        // populated on first focus of context search
let selectedContextFiles = [];       // array of {task_id, output_path, description_preview}

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    setupEventListeners();
    startPolling();
    updateLogs('orchestrator');
    // Keep "X ago" display fresh between poll cycles
    setInterval(updateLastUpdateTime, 1000);
});

// Setup event listeners
function setupEventListeners() {
    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            switchTab(e.target.dataset.tab);
        });
    });

    // Chat input - Enter to send, Shift+Enter for line break
    const chatInput = document.getElementById('chat-input');
    if (chatInput) {
        chatInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendChatMessage();
            }
            // Shift+Enter falls through to default textarea behaviour (line break)
        });
    }

    // History filter
    document.getElementById('history-filter').addEventListener('change', updateHistoryTasks);

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

        // Update logs if visible — respect the selected log type so the poll
        // loop doesn't clobber Ollama API logs with agent logs every cycle.
        if (document.getElementById('tab-logs').classList.contains('active')) {
            const logTypeSelect = document.getElementById('log-type-select');
            const selectedLogType = logTypeSelect ? logTypeSelect.value : 'agent';
            if (selectedLogType === 'ollama') {
                const sessionSelect = document.getElementById('log-session-select');
                await loadOllamaLogs(sessionSelect ? sessionSelect.value : '');
            } else {
                const selectedAgent = document.getElementById('log-agent-select').value || 'orchestrator';
                await updateLogs(selectedAgent);
            }
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

        // Set up event delegation for approve/reject buttons
        setupApprovalButtons(container);
    } catch (error) {
        console.error('Error updating approvals:', error);
    }
}

// Set up event delegation for approval action buttons
function setupApprovalButtons(container) {
    container.addEventListener('click', (event) => {
        const button = event.target.closest('button[data-action]');
        if (!button) return;

        event.stopPropagation();
        const taskId = button.getAttribute('data-task-id');
        const action = button.getAttribute('data-action');

        if (!taskId) {
            console.error('Missing task ID on approval button');
            return;
        }

        if (action === 'approve') {
            approveTask(taskId);
        } else if (action === 'reject') {
            rejectTask(taskId);
        }
    });
}

// Create approval task element HTML
function createApprovalTaskElement(task) {
    const ageStr = formatAge(task.age_seconds);
    const priClass = task.priority === 'high' ? 'pri-high' : task.priority === 'medium' ? 'pri-medium' : '';
    const bodyPreview = task.body ? task.body.slice(0, 160) : '';

    return `
        <div class="task-item t-approval" data-task-id="${task.id}">
            <div class="task-row">
                <span class="expand-placeholder"></span>
                <span class="task-id">${task.id}</span>
                <span class="tag">${task.type}</span>
                <span class="tag ${priClass}">${task.priority}</span>
                <span class="spacer"></span>
                <span class="task-status approval">approval</span>
                <span class="time-ago">${ageStr}</span>
            </div>
            ${bodyPreview ? `<div class="task-desc">${escapeHtml(bodyPreview)}</div>` : ''}
            <div class="task-meta-row">
                <span class="agent-tag">→ ${task.assigned_to}</span>
                <span class="subtask-prog">${task.created_by}</span>
            </div>
            <div class="approval-actions">
                <button class="btn-approve" data-task-id="${task.id}" data-action="approve">✓ Approve</button>
                <button class="btn-reject" data-task-id="${task.id}" data-action="reject">✕ Reject</button>
            </div>
        </div>
    `;
}

// Approve task
async function approveTask(taskId) {
    try {
        const response = await fetch(`/api/pending-approvals/${taskId}/approve`, withAuth({
            method: 'POST',
        }));
        
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
        const response = await fetch(`/api/pending-approvals/${taskId}/reject`, withAuth({
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({ reason: reason || 'Rejected by user' }),
        }));
        
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

// Agent icons map
const AGENT_ICONS = {
    orchestrator: '⬡',
    coder:        '⌨',
    research:     '🔍',
    qa:           '✓',
    'claude-code':'◆',
    scheduler:    '⏱',
};

// Update agent statistics
async function updateAgentStats() {
    try {
        const response = await fetch('/api/agents');
        const stats = await response.json();

        const tbody = document.getElementById('agent-stats-body');
        const rows = Object.entries(stats).map(([agent, data]) => {
            const promptTokens     = data.prompt_tokens     || 0;
            const completionTokens = data.completion_tokens || 0;
            const llmCalls         = data.llm_calls         || 0;
            const completed        = data.completed         || 0;
            const errors           = data.errors            || 0;

            // Show "—" for claude-code if all token values are 0
            const showDash = agent === 'claude-code' && promptTokens === 0 && completionTokens === 0 && llmCalls === 0;

            const promptDisplay     = showDash ? '—' : promptTokens.toLocaleString();
            const completionDisplay = showDash ? '—' : completionTokens.toLocaleString();
            const callsDisplay      = showDash ? '—' : llmCalls.toLocaleString();

            const icon = AGENT_ICONS[agent] || '·';

            return `
                <tr>
                    <td>
                        <div class="agent-cell">
                            <div class="agent-icon">${icon}</div>
                            <strong>${agent}</strong>
                        </div>
                    </td>
                    <td class="mono-cell ${completed > 0 ? 'good' : 'zero'}">${completed}</td>
                    <td class="mono-cell ${errors > 0 ? 'error' : 'zero'}">${errors}</td>
                    <td class="mono-cell ${promptTokens === 0 ? 'zero' : ''}">${promptDisplay}</td>
                    <td class="mono-cell ${completionTokens === 0 ? 'zero' : ''}">${completionDisplay}</td>
                    <td class="mono-cell ${llmCalls === 0 ? 'zero' : ''}">${callsDisplay}</td>
                </tr>
            `;
        }).join('');

        tbody.innerHTML = rows || '<tr><td colspan="6" class="no-data">No agents</td></tr>';
    } catch (error) {
        console.error('Error updating agent stats:', error);
    }
}

// Parse a raw log line into structured parts (timestamp, level, agent, message)
// Expected format: "2026-05-16 10:23:45,123 INFO [orchestrator] message"
const LOG_RE = /^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[,.]?\d*)\s+(INFO|WARNING|WARN|ERROR|CRITICAL|DEBUG)\s+\[([^\]]+)\]\s+([\s\S]*)$/;

function renderLogLine(raw) {
    const m = raw.match(LOG_RE);
    if (!m) {
        // Fall back to plain mono entry
        return `<div class="log-entry">${escapeHtml(raw)}</div>`;
    }
    const [, ts, lvl, agent, msg] = m;
    const lvlNorm = lvl === 'WARNING' ? 'WARN' : lvl === 'CRITICAL' ? 'ERROR' : lvl;
    const lvlClass = lvlNorm === 'INFO' ? 'log-info' : lvlNorm === 'WARN' ? 'log-warn' : lvlNorm === 'ERROR' ? 'log-error' : 'log-debug';
    return `<div class="log-line">
        <span class="log-ts">${escapeHtml(ts)}</span>
        <span class="${lvlClass}">${lvlNorm}</span>
        <span class="log-lagent">${escapeHtml(agent)}</span>
        <span class="log-msg">${escapeHtml(msg)}</span>
    </div>`;
}

// Tracks the last-rendered log markup so polling can skip identical re-renders
// (re-rendering every 2s reset the user's scroll position and made logs unusable).
let _lastLogsHtml = '';

// Update logs for a specific agent
async function updateLogs(agent) {
    try {
        const response = await fetch(`/api/agents/${agent}/logs?lines=50`);
        const data = await response.json();

        const container = document.getElementById('logs-list');
        const scroller = container.parentElement;

        if (!data.logs || data.logs.length === 0) {
            const emptyHtml = '<p class="no-data">No logs available for ' + agent + '</p>';
            if (container.innerHTML !== emptyHtml) container.innerHTML = emptyHtml;
            _lastLogsHtml = '';
            return;
        }

        const html = [...data.logs].reverse().map(renderLogLine).join('');
        const kindKey = 'agent:' + agent;
        const kindChanged = container.dataset.logKind !== kindKey;

        // Unchanged content for the same view — leave the DOM (and scroll) alone.
        if (html === _lastLogsHtml && !kindChanged) return;

        const prevTop = scroller.scrollTop;
        const prevHeight = scroller.scrollHeight;
        container.innerHTML = html;
        container.dataset.logKind = kindKey;
        _lastLogsHtml = html;

        // Reset to top only when the view itself changed (agent switch / tab open).
        // On a routine poll refresh, preserve the user's view: new entries are
        // prepended at the top, so add the height gained above to scrollTop to
        // keep the same log lines in place instead of letting them shift down.
        scroller.scrollTop = kindChanged
            ? 0
            : prevTop + (scroller.scrollHeight - prevHeight);
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

// Handle local file picker selection — upload files and add as chips
async function handleContextFilePicker(input) {
    if (!input.files || input.files.length === 0) return;

    const uploadingEl = document.getElementById('context-file-uploading');
    const btn = document.querySelector('.btn-browse-files');
    uploadingEl.style.display = 'block';
    if (btn) { btn.disabled = true; btn.textContent = 'Uploading…'; }

    try {
        const formData = new FormData();
        for (const file of input.files) {
            formData.append('files', file);
        }

        const resp = await fetch('/api/upload-context', withAuth({
            method: 'POST',
            body: formData,
        }));
        const data = await resp.json();

        if (!resp.ok) {
            showNotification(`Upload failed: ${data.error || 'unknown error'}`, 'error');
            return;
        }

        for (const f of data.uploaded || []) {
            // Use "file__{saved_as}" as the unique key so it won't collide with task IDs
            addContextFile(`file__${f.saved_as}`, f.path, `📄 ${f.name}`);
        }

        if (data.errors && data.errors.length > 0) {
            showNotification(`Some files skipped: ${data.errors.join(', ')}`, 'error');
        } else {
            showNotification(`${data.uploaded.length} file(s) added`, 'success');
        }
    } catch (e) {
        showNotification(`Upload error: ${e.message}`, 'error');
    } finally {
        uploadingEl.style.display = 'none';
        if (btn) { btn.disabled = false; btn.textContent = '📁 Browse files'; }
        // Reset so the same file can be picked again if needed
        input.value = '';
    }
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
        const response = await fetch('/api/tasks/submit', withAuth({
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
        }));

        const data = await response.json();

        if (response.ok) {
            showSubmitStatus(`Task submitted: ${data.task_id}`, 'success');
            document.getElementById('task-description').value = '';
            document.getElementById('task-expected-output').value = '';
            // Reset context files picker
            selectedContextFiles = [];
            renderContextChips();
            document.getElementById('context-file-input').value = '';
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
    const isSubtask = !!task.parent_task_id;
    const subtasksDisplay = isParent && !isExpanded ? 'style="display: none;"' : '';
    const statusClass = task.status.toLowerCase();

    // Priority accent class
    const priClass = task.priority === 'high' ? 'pri-high' : task.priority === 'medium' ? 'pri-medium' : '';

    // Subtask counts for parent
    const total     = isParent ? task.subtasks.length : 0;
    const doneCount = isParent ? task.subtasks.filter(s => s.status === 'completed').length : 0;

    const classes = `task-item t-${statusClass}${isSubtask ? ' subtask' : ''}`;

    let html = `
        <div class="${classes}" data-task-id="${task.id}">
            <div class="task-row">
                ${isParent
                    ? `<button class="expand-toggle ${isExpanded ? 'expanded' : ''}">▶</button>`
                    : '<span class="expand-placeholder"></span>'}
                <span class="task-id">${task.id}</span>
                <span class="tag">${task.type}</span>
                <span class="tag ${priClass}">${task.priority}</span>
                <span class="spacer"></span>
                <span class="task-status ${statusClass}">${task.status}</span>
                <span class="time-ago">${formatAge(task.age_seconds)}</span>
            </div>`;

    // Body preview line
    if (task.body_preview) {
        let preview = task.body_preview.replace(/^##\s+Task Description\n/, '').trim();
        html += `<div class="task-desc">${escapeHtml(preview.slice(0, 140))}</div>`;
    }

    html += `
            <div class="task-meta-row">
                <span class="agent-tag">→ ${task.assigned_to}</span>
                ${isParent ? `<span class="subtask-prog">${doneCount}/${total} subtasks</span>` : ''}
                ${task.retry_count > 0 ? `<span class="tag">retry&nbsp;${task.retry_count}</span>` : ''}
                ${task.iteration ? `<span class="tag">iter&nbsp;${task.iteration}</span>` : ''}
            </div>
        </div>`;

    // Subtasks container
    if (isParent) {
        const subtasksHtml = task.subtasks.map(subtask => renderTaskWithChildren(subtask, depth + 1)).join('');
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

        if (task.result) {
            html += `
                <div class="detail-section">
                    <h4>Result</h4>
                    <pre class="detail-result">${escapeHtml(task.result)}</pre>
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
        // Determine which logs to load based on the selected log type
        const logTypeSelect = document.getElementById('log-type-select');
        const selectedLogType = logTypeSelect ? logTypeSelect.value : 'agent';

        if (selectedLogType === 'ollama') {
            const sessionSelect = document.getElementById('log-session-select');
            const selectedSession = sessionSelect ? sessionSelect.value : '';
            loadOllamaLogs(selectedSession);
        } else {
            const selectedAgent = document.getElementById('log-agent-select').value || 'orchestrator';
            updateLogs(selectedAgent);
        }
    } else if (tabName === 'knowledge') {
        loadKnowledgeBase();
    } else if (tabName === 'chat') {
        loadChatModels();
    }
}

// Update last update timestamp (relative "Xs ago" format)
function updateLastUpdateTime() {
    const el = document.getElementById('last-update');
    if (!lastUpdate) { el.textContent = '—'; return; }
    const elapsed = Math.floor((Date.now() - lastUpdate.getTime()) / 1000);
    if (elapsed < 5)        el.textContent = 'just now';
    else if (elapsed < 60)  el.textContent = `${elapsed}s ago`;
    else if (elapsed < 3600) el.textContent = `${Math.floor(elapsed / 60)}m ago`;
    else                    el.textContent = `${Math.floor(elapsed / 3600)}h ago`;
}

// Set poll status indicator
function setPollStatus(success) {
    const status = document.getElementById('poll-status');
    if (success) {
        status.classList.remove('error');
        status.textContent = 'Live';
    } else {
        status.classList.add('error');
        status.textContent = 'Error';
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
        const response = await fetch('/api/clear-cache', withAuth({
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        }));

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

// ---------------------------------------------------------------------------
// Knowledge Base
// ---------------------------------------------------------------------------

// Read a file from disk and populate the KB ingest form
function kbLoadFileIntoForm(input) {
    if (!input.files || input.files.length === 0) return;
    const file = input.files[0];

    // Accept text-based files only
    const textTypes = [
        'text/', 'application/json', 'application/javascript',
        'application/xml', 'application/x-yaml', 'application/x-sh',
        'application/x-python',
    ];
    const isText = textTypes.some(t => file.type.startsWith(t)) || file.type === '';

    if (!isText) {
        showNotification(`Unsupported file type "${file.type || 'binary'}" — use plain text, markdown, code, or JSON files.`, 'error');
        input.value = '';
        return;
    }

    const reader = new FileReader();
    reader.onload = (e) => {
        const content = e.target.result;

        // Populate content textarea
        document.getElementById('kb-doc-content').value = content;

        // Auto-fill title if empty
        const titleEl = document.getElementById('kb-doc-title');
        if (!titleEl.value.trim()) {
            // Strip extension for a readable title
            titleEl.value = file.name.replace(/\.[^.]+$/, '');
        }

        // Auto-fill source with filename
        const sourceEl = document.getElementById('kb-doc-source');
        if (!sourceEl.value.trim()) {
            sourceEl.value = file.name;
        }

        // Show confirmation banner
        const loaded = document.getElementById('kb-file-loaded');
        loaded.style.display = 'block';
        loaded.innerHTML = `📄 Loaded <strong>${escapeHtml(file.name)}</strong> (${(file.size / 1024).toFixed(1)} KB) — review below then click <em>Add to Knowledge Base</em>.`;

        // Reset input so the same file can be reloaded if needed
        input.value = '';
    };
    reader.onerror = () => {
        showNotification(`Error reading file: ${file.name}`, 'error');
        input.value = '';
    };
    reader.readAsText(file);
}

async function loadKnowledgeBase() {
    const list = document.getElementById('kb-docs-list');
    const badge = document.getElementById('kb-status-badge');

    // Check RAG API status
    try {
        const statusResp = await fetch('/api/rag/status');
        if (statusResp.status === 503) {
            badge.textContent = '● Unavailable';
            badge.className = 'kb-status kb-status-down';
            list.innerHTML = '<p class="no-data">RAG API is not running. Start the scheduler to launch it automatically.</p>';
            return;
        }
        badge.textContent = '● Online';
        badge.className = 'kb-status kb-status-up';
    } catch (e) {
        badge.textContent = '● Unavailable';
        badge.className = 'kb-status kb-status-down';
        list.innerHTML = '<p class="no-data">RAG API is not reachable.</p>';
        return;
    }

    // Load documents
    list.innerHTML = '<p class="no-data">Loading...</p>';
    try {
        const resp = await fetch('/api/rag/documents');
        const data = await resp.json();
        const docs = data.documents || [];
        if (docs.length === 0) {
            list.innerHTML = '<p class="no-data">No documents in the knowledge base yet.</p>';
            return;
        }

        // Group chunks by title + source so each document appears as one row
        const groups = {};
        for (const doc of docs) {
            const title = doc.metadata?.title || 'Untitled';
            const source = doc.metadata?.source || '';
            const key = `${title}||${source}`;
            if (!groups[key]) groups[key] = { title, source, ids: [] };
            groups[key].ids.push(doc.id);
        }

        list.innerHTML = Object.values(groups).map(g => {
            const chunkLabel = `${g.ids.length} chunk${g.ids.length !== 1 ? 's' : ''}`;
            // Encode ids for an HTML attribute — must escape " to avoid closing the attribute early
            const safeIds = JSON.stringify(g.ids).replace(/"/g, '&quot;');
            return `
                <div class="kb-doc-item">
                    <div class="kb-doc-info">
                        <span class="kb-doc-title">${escapeHtml(g.title)}</span>
                        <span class="kb-doc-meta">${escapeHtml(g.source)}${g.source ? ' · ' : ''}${chunkLabel}</span>
                    </div>
                    <button class="btn-kb-delete"
                        data-title="${escapeHtml(g.title)}"
                        data-ids="${safeIds}"
                        onclick="kbDeleteDocumentGroup(this)">✕ Delete</button>
                </div>
            `;
        }).join('');
    } catch (e) {
        list.innerHTML = `<p class="no-data">Error loading documents: ${escapeHtml(e.message)}</p>`;
    }
}

async function kbIngestDocument() {
    const title = document.getElementById('kb-doc-title').value.trim();
    const source = document.getElementById('kb-doc-source').value.trim();
    const content = document.getElementById('kb-doc-content').value.trim();
    const statusEl = document.getElementById('kb-ingest-status');
    const msgEl = document.getElementById('kb-ingest-message');
    const btn = document.getElementById('kb-ingest-btn');

    if (!content) {
        statusEl.style.display = 'block';
        msgEl.className = 'submit-message error';
        msgEl.textContent = 'Content is required.';
        return;
    }

    btn.disabled = true;
    btn.textContent = 'Adding...';
    statusEl.style.display = 'none';

    try {
        // Generate a unique document_id (required by IngestRequest)
        const document_id = `doc_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
        const body = { document_id, content, metadata: { title: title || 'Untitled', source: source || '' } };
        const resp = await fetch('/api/rag/ingest', withAuth({
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        }));
        const data = await resp.json();
        if (!resp.ok) {
            // FastAPI validation errors return detail as an array of objects
            let errMsg = data.error || 'Ingest failed';
            if (data.detail) {
                errMsg = Array.isArray(data.detail)
                    ? data.detail.map(e => `${e.loc?.join('.')||''}: ${e.msg||e}`).join('; ')
                    : String(data.detail);
            }
            throw new Error(errMsg);
        }

        statusEl.style.display = 'block';
        msgEl.className = 'submit-message success';
        msgEl.textContent = `✓ Document added (${data.chunks_created || data.document_ids?.length || 1} chunk(s))`;

        // Clear form
        document.getElementById('kb-doc-title').value = '';
        document.getElementById('kb-doc-source').value = '';
        document.getElementById('kb-doc-content').value = '';
        const loadedBanner = document.getElementById('kb-file-loaded');
        if (loadedBanner) loadedBanner.style.display = 'none';

        // Refresh list
        setTimeout(loadKnowledgeBase, 500);
    } catch (e) {
        statusEl.style.display = 'block';
        msgEl.className = 'submit-message error';
        msgEl.textContent = `✗ Error: ${e.message}`;
    } finally {
        btn.disabled = false;
        btn.textContent = 'Add to Knowledge Base';
    }
}

async function kbDeleteDocumentGroup(btn) {
    const title = btn.getAttribute('data-title');
    const ids = JSON.parse(btn.getAttribute('data-ids'));
    const chunkWord = ids.length !== 1 ? 'chunks' : 'chunk';
    if (!confirm(`Delete "${title}" (${ids.length} ${chunkWord})?`)) return;

    btn.disabled = true;
    btn.textContent = 'Deleting…';
    try {
        // Delete all chunks in parallel
        const results = await Promise.allSettled(
            ids.map(id => fetch(`/api/rag/documents/${encodeURIComponent(id)}`, withAuth({ method: 'DELETE' })))
        );
        const failures = results.filter(r => r.status === 'rejected').length;
        if (failures > 0) {
            showNotification(`Deleted with ${failures} error(s) — refreshing`, 'error');
        } else {
            showNotification(`Deleted "${title}" (${ids.length} ${chunkWord})`, 'success');
        }
        loadKnowledgeBase();
    } catch (e) {
        showNotification(`Delete failed: ${e.message}`, 'error');
        btn.disabled = false;
        btn.textContent = '✕ Delete';
    }
}

async function kbDeleteDocument(docId) {
    // Legacy single-chunk delete (kept for compatibility)
    try {
        await fetch(`/api/rag/documents/${encodeURIComponent(docId)}`, withAuth({ method: 'DELETE' }));
        loadKnowledgeBase();
    } catch (e) {
        showNotification(`Delete failed: ${e.message}`, 'error');
    }
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

// ============================================================================
// Chat Functions
// ============================================================================

// ============================================================================
// Chat Functions
// ============================================================================

let chatSessionId = null;
let chatThinkingMode = false;
let chatSelectedModel = null; // Current selected model
let chatActiveStream = null;   // AbortController for the in-flight stream
let _streamThinkingAccum = ''; // per-message thinking accumulator
let _streamContentAccum  = ''; // per-message content accumulator
let _chatModelsLoaded = false; // Track if models have been loaded

async function loadChatModels() {
    if (_chatModelsLoaded) return;
    try {
        const resp = await fetch('/api/models');
        const data = await resp.json();
        const models = data.models || [];

        const select = document.getElementById('chat-model-select');
        if (!select || models.length === 0) return;

        // Populate dropdown
        select.innerHTML = '';
        models.forEach(model => {
            const option = document.createElement('option');
            option.value = model.name;
            option.textContent = model.label;
            option.dataset.isDefault = model.is_default;
            select.appendChild(option);
        });

        // Restore from localStorage or use default
        const savedModel = localStorage.getItem('chatSelectedModel');
        if (savedModel && select.querySelector(`option[value="${savedModel}"]`)) {
            select.value = savedModel;
            chatSelectedModel = savedModel;
        } else {
            const defaultModel = models.find(m => m.is_default);
            if (defaultModel) {
                select.value = defaultModel.name;
                chatSelectedModel = defaultModel.name;
            }
        }

        // Attach change handler
        select.addEventListener('change', handleModelChange);
        _chatModelsLoaded = true;
    } catch (err) {
        console.error('Failed to load chat models:', err);
    }
}

function handleModelChange(event) {
    chatSelectedModel = event.target.value;
    localStorage.setItem('chatSelectedModel', chatSelectedModel);
    console.log('Selected model:', chatSelectedModel);
}

function toggleThinkingMode() {
    chatThinkingMode = !chatThinkingMode;
    const toggle = document.getElementById('chat-think-toggle');
    const icon   = document.getElementById('think-icon');
    toggle.classList.toggle('active', chatThinkingMode);
    if (icon) icon.textContent = chatThinkingMode ? '🧠' : '⚡';
    toggle.title = chatThinkingMode
        ? 'Thinking: deep reasoning for complex questions. Click to switch to Standard.'
        : 'Standard: fast responses. Click to switch to Thinking.';
}

// ── marked setup (runs once) ─────────────────────────────────────────────
// We let marked render code blocks normally and apply hljs AFTER the HTML is
// inserted into the DOM via hljs.highlightElement(). This avoids the marked
// v11 renderer API ambiguity entirely and is the approach hljs itself recommends.
(function initMarked() {
    marked.use({ breaks: true, gfm: true });
})();

function markdownToHtml(text) {
    const html = marked.parse(text);
    // span is allowed so hljs spans survive if any are already present.
    return DOMPurify.sanitize(html, {
        ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'span',
                       'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                       'ul', 'ol', 'li', 'blockquote', 'a', 'img',
                       'table', 'thead', 'tbody', 'tr', 'td', 'th', 'hr'],
        ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'class', 'style'],
        ALLOW_DATA_ATTR: false,
    });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function sendChatMessage() {
    const input = document.getElementById('chat-input');
    const message = input.value.trim();
    if (!message) return;

    // Cancel any in-flight stream
    if (chatActiveStream) {
        chatActiveStream.abort();
        chatActiveStream = null;
    }

    // Reset per-message accumulators
    _streamThinkingAccum = '';
    _streamContentAccum  = '';

    input.disabled = true;
    const sendBtn = document.getElementById('chat-send-btn');
    sendBtn.disabled = true;

    appendChatBubble('user', message);
    input.value = '';

    const now = new Date();
    const isoTs = now.toISOString().slice(0, 16).replace('T', ' ') + ' ' +
        Intl.DateTimeFormat().resolvedOptions().timeZone;

    const controller = new AbortController();
    chatActiveStream = controller;

    // Create the assistant bubble structure upfront
    const { toolsEl, thinkingEl, contentEl } = createStreamingBubble();

    const requestBody = {
        message,
        session_id: chatSessionId,
        timestamp: isoTs,
        thinking_mode: chatThinkingMode,
    };
    if (chatSelectedModel) {
        requestBody.model = chatSelectedModel;
    }

    fetch('/api/chat/stream', withAuth({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
        signal: controller.signal,
    }))
    .then(resp => {
        if (!resp.ok) {
            return resp.json().then(data => {
                throw new Error(data.error || 'Stream failed');
            });
        }
        const reader  = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        function readChunk() {
            reader.read().then(({ done, value }) => {
                if (done) return;
                buffer += decoder.decode(value, { stream: true });
                // SSE events are delimited by double newlines
                const parts = buffer.split('\n\n');
                buffer = parts.pop(); // keep the trailing incomplete chunk
                for (const part of parts) {
                    if (!part.trim()) continue;
                    for (const line of part.split('\n')) {
                        if (!line.startsWith('data: ')) continue;
                        const raw = line.slice(6).trim();
                        if (raw === '[DONE]') return;
                        try {
                            handleStreamEvent(
                                JSON.parse(raw),
                                toolsEl, thinkingEl, contentEl
                            );
                        } catch (_) { /* skip malformed JSON */ }
                    }
                }
                readChunk();
            }).catch(err => {
                if (err.name !== 'AbortError') {
                    contentEl.classList.remove('streaming');
                    contentEl.classList.add('chat-stream-error');
                    contentEl.textContent = `Stream error: ${err.message}`;
                }
            });
        }
        readChunk();
    })
    .catch(err => {
        if (err.name !== 'AbortError') {
            contentEl.classList.remove('streaming');
            contentEl.classList.add('chat-stream-error');
            contentEl.textContent = `Error: ${err.message}`;
        }
    })
    .finally(() => {
        chatActiveStream = null;
        input.disabled = false;
        sendBtn.disabled = false;
        input.focus();
    });
}

// Handle a single SSE event object
function handleStreamEvent(event, toolsEl, thinkingEl, contentEl) {
    const chatContainer = document.getElementById('chat-messages');

    if (event.type === 'meta') {
        if (event.session_id) chatSessionId = event.session_id;
        return;
    }

    if (event.type === 'tool_call') {
        addToolCallPill(toolsEl, event.name, event.args || {});
        return;
    }

    if (event.type === 'thinking') {
        contentEl.classList.remove('waiting');
        _streamThinkingAccum += event.text;
        updateThinkingBlock(thinkingEl, _streamThinkingAccum);
        return;
    }

    if (event.type === 'token') {
        contentEl.classList.remove('waiting');
        _streamContentAccum += event.text;
        // Raw text while streaming; ::after pseudo-element adds the cursor
        contentEl.textContent = _streamContentAccum;
        contentEl.classList.add('streaming');
        chatContainer.scrollTop = chatContainer.scrollHeight;
        return;
    }

    if (event.type === 'done') {
        contentEl.classList.remove('waiting');
        contentEl.classList.remove('streaming');
        // Render final markdown (replaces raw streamed text)
        const finalContent = event.full_content || _streamContentAccum;
        contentEl.classList.add('rendered');
        contentEl.innerHTML = markdownToHtml(finalContent);
        contentEl.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));
        // Finalise thinking block label
        if (_streamThinkingAccum) {
            const summary = thinkingEl.querySelector('.chat-thinking-summary');
            if (summary) summary.textContent = '💭 Thinking';
        }
        // Task badge
        if (event.action && event.action.type === 'task_created') {
            appendTaskBadge(event.action.task_id);
        }
        _streamThinkingAccum = '';
        _streamContentAccum  = '';
        chatContainer.scrollTop = chatContainer.scrollHeight;
        return;
    }

    if (event.type === 'error') {
        contentEl.classList.remove('waiting');
        contentEl.classList.remove('streaming');
        contentEl.classList.add('chat-stream-error');
        contentEl.textContent = `Error: ${event.message}`;
        _streamThinkingAccum = '';
        _streamContentAccum  = '';
        return;
    }
}

// Build the assistant streaming bubble and return its sub-elements
function createStreamingBubble() {
    const container = document.getElementById('chat-messages');

    const msg = document.createElement('div');
    msg.className = 'chat-msg assistant';

    const label = document.createElement('span');
    label.className = 'chat-label';
    label.textContent = 'LLM';

    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';

    // Tool calls row (hidden until first tool fires)
    const toolsEl = document.createElement('div');
    toolsEl.className = 'chat-tool-calls';
    toolsEl.style.display = 'none';

    // Collapsible thinking block (hidden until first thinking chunk)
    const thinkingEl = document.createElement('details');
    thinkingEl.className = 'chat-thinking';
    thinkingEl.style.display = 'none';
    const thinkingSummary = document.createElement('summary');
    thinkingSummary.className = 'chat-thinking-summary';
    thinkingSummary.textContent = '💭 Thinking…';
    const thinkingBody = document.createElement('div');
    thinkingBody.className = 'chat-thinking-body';
    thinkingEl.appendChild(thinkingSummary);
    thinkingEl.appendChild(thinkingBody);

    // Main content area — starts in "waiting" state (animated dots)
    const contentEl = document.createElement('div');
    contentEl.className = 'chat-bubble-content waiting';

    bubble.appendChild(toolsEl);
    bubble.appendChild(thinkingEl);
    bubble.appendChild(contentEl);

    const time = document.createElement('span');
    time.className = 'chat-time';
    time.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

    msg.appendChild(label);
    msg.appendChild(bubble);
    msg.appendChild(time);
    container.appendChild(msg);
    container.scrollTop = container.scrollHeight;

    return { msgEl: msg, toolsEl, thinkingEl, contentEl };
}

// Add a tool-call pill to the tools row
function addToolCallPill(toolsEl, toolName, args) {
    toolsEl.style.display = 'flex';
    const pill = document.createElement('span');
    pill.className = 'tool-pill';
    const icons = { rag_query: '📚', web_search: '🔍', web_fetch: '🌐' };
    pill.textContent = (icons[toolName] || '🔧') + ' ' + toolName.replace(/_/g, ' ');
    const argStr = Object.entries(args)
        .map(([k, v]) => `${k}: ${String(v).slice(0, 80)}`)
        .join(', ');
    if (argStr) pill.title = argStr;
    toolsEl.appendChild(pill);
    document.getElementById('chat-messages').scrollTop =
        document.getElementById('chat-messages').scrollHeight;
}

// Update the collapsible thinking block with accumulated text
function updateThinkingBlock(thinkingEl, text) {
    thinkingEl.style.display = 'block';
    const body = thinkingEl.querySelector('.chat-thinking-body');
    if (body) body.textContent = text;
    document.getElementById('chat-messages').scrollTop =
        document.getElementById('chat-messages').scrollHeight;
}

function appendChatBubble(role, content) {
    const container = document.getElementById('chat-messages');

    // Wrapper: .chat-msg.user / .chat-msg.assistant / .chat-msg.error
    const msgRole = role === 'error' ? 'assistant' : role;
    const msg = document.createElement('div');
    msg.className = `chat-msg ${msgRole}`;

    // Label above the bubble
    const label = document.createElement('span');
    label.className = 'chat-label';
    label.textContent = role === 'user' ? 'You' : 'LLM';

    // Bubble
    const bubble = document.createElement('div');
    bubble.className = role === 'error' ? 'chat-bubble chat-bubble-error' : 'chat-bubble';

    // Render content: user messages as plain text, assistant/error as markdown
    if (role === 'user') {
        bubble.textContent = content;
    } else {
        bubble.innerHTML = markdownToHtml(content);
        // Apply syntax highlighting to each fenced code block.
        // hljs.highlightElement() operates on the live DOM node so it bypasses
        // the marked renderer API entirely — works on any marked version.
        bubble.querySelectorAll('pre code').forEach(el => {
            hljs.highlightElement(el);
        });
    }

    // Timestamp
    const time = document.createElement('span');
    time.className = 'chat-time';
    time.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

    msg.appendChild(label);
    msg.appendChild(bubble);
    msg.appendChild(time);
    container.appendChild(msg);

    // Scroll to bottom
    container.scrollTop = container.scrollHeight;
}

function appendTaskBadge(taskId) {
    const container = document.getElementById('chat-messages');
    const badge = document.createElement('div');
    badge.className = 'chat-task-badge';
    badge.textContent = `✓ Task created: ${taskId}`;
    container.appendChild(badge);
    container.scrollTop = container.scrollHeight;
}

function clearChatHistory() {
    if (!chatSessionId) return;

    if (!confirm('Clear chat history?')) return;

    fetch('/api/chat/clear', withAuth({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: chatSessionId }),
    }))
    .then(() => {
        document.getElementById('chat-messages').innerHTML = '';
        chatSessionId = null;
        showNotification('Chat history cleared', 'success');
    })
    .catch(err => {
        showNotification(`Error: ${err.message}`, 'error');
    });
}

// ─────────────────────────────────────────────────────────────────────────────
// Ollama API Logging
// ─────────────────────────────────────────────────────────────────────────────

let currentLogType = 'agent';
let loadedSessionIds = new Set();

function switchLogType(type) {
    currentLogType = type;
    const agentSelect = document.getElementById('log-agent-select');
    const sessionSelect = document.getElementById('log-session-select');

    if (type === 'agent') {
        agentSelect.style.display = 'inline-block';
        sessionSelect.style.display = 'none';
        loadAgentLogs(agentSelect.value);
    } else if (type === 'ollama') {
        agentSelect.style.display = 'none';
        sessionSelect.style.display = 'inline-block';
        loadOllamaLogs(sessionSelect.value);
    }
}

async function loadAgentLogs(agent) {
    currentLogType = 'agent';
    await updateLogs(agent);
}

async function loadOllamaLogs(sessionId) {
    try {
        const url = sessionId ? `/api/ollama/logs/${sessionId}?limit=100` : '/api/ollama/logs?limit=100';
        const response = await fetch(url);
        const data = await response.json();

        const container = document.getElementById('logs-list');
        const scroller = container.parentElement;

        if (!data.logs || data.logs.length === 0) {
            const emptyHtml = '<p class="no-data">No Ollama API logs available</p>';
            if (container.innerHTML !== emptyHtml) container.innerHTML = emptyHtml;
            _lastLogsHtml = '';
            return;
        }

        // Collect unique session IDs for the dropdown
        const sessionIds = new Set();
        data.logs.forEach(log => {
            if (log.session_id && log.session_id !== 'unknown') {
                sessionIds.add(log.session_id);
            }
        });

        // Update session dropdown — only rebuild when the option set actually
        // changed, so polling doesn't reset/close the dropdown every 2s.
        const sessionSelect = document.getElementById('log-session-select');
        const currentValue = sessionSelect.value;
        const sortedIds = Array.from(sessionIds).sort().reverse();
        const optionsKey = sortedIds.join('|');
        if (sessionSelect.dataset.optionsKey !== optionsKey) {
            const newOptions = ['<option value="">All Sessions</option>'];
            sortedIds.forEach(sid => {
                newOptions.push(`<option value="${sid}" ${currentValue === sid ? 'selected' : ''}>${sid.substring(0, 8)}...</option>`);
            });
            sessionSelect.innerHTML = newOptions.join('');
            sessionSelect.dataset.optionsKey = optionsKey;
            // Restore the selection in case the rebuild dropped it
            sessionSelect.value = currentValue;
        }

        // Group each request with its responses + stream chunks, newest group first.
        const html = renderOllamaLogGroups(data.logs);
        const kindKey = 'ollama:' + (sessionId || '');
        const kindChanged = container.dataset.logKind !== kindKey;

        // Unchanged content for the same view — leave the DOM (and scroll) alone.
        if (html === _lastLogsHtml && !kindChanged) return;

        const prevTop = scroller.scrollTop;
        const prevHeight = scroller.scrollHeight;
        container.innerHTML = html;
        container.dataset.logKind = kindKey;
        _lastLogsHtml = html;

        // Reset to top only when the view changed (session filter / tab open).
        // On a routine poll refresh, preserve the user's view: new entries are
        // prepended at the top, so add the height gained above to scrollTop to
        // keep the same entries in place instead of letting them shift down.
        scroller.scrollTop = kindChanged
            ? 0
            : prevTop + (scroller.scrollHeight - prevHeight);
    } catch (error) {
        console.error('Error loading Ollama logs:', error);
        const container = document.getElementById('logs-list');
        container.innerHTML = '<p class="no-data">Error loading Ollama logs: ' + error.message + '</p>';
    }
}

// Group a flat, chronological log list into per-request groups. Each `request`
// starts a new group; subsequent `response` / `stream_chunk` / `error` entries
// attach to it. Groups are rendered newest-first (within-group order preserved).
function renderOllamaLogGroups(logs) {
    const groups = [];
    let current = null;
    for (const entry of logs) {
        if (entry.direction === 'request' || !current) {
            current = { request: null, items: [] };
            groups.push(current);
        }
        if (entry.direction === 'request') {
            current.request = entry;
        } else {
            current.items.push(entry);
        }
    }
    return groups.reverse().map(renderOllamaGroup).join('');
}

function renderOllamaGroup(group) {
    const req = group.request;
    const anchor = req || group.items[0] || null;
    const ts = anchor ? new Date(anchor.timestamp).toLocaleTimeString() : '';
    const sid = (anchor && anchor.session_id) ? anchor.session_id : 'unknown';
    const sessionShort = (sid && sid !== 'unknown') ? sid.substring(0, 8) + '…' : 'unknown';
    const model = (req && req.payload && req.payload.model) ? req.payload.model : '';

    const responses = group.items.filter(i => i.direction === 'response');
    const streams = group.items.filter(i => i.direction === 'stream_chunk');
    const errors = group.items.filter(i => i.direction === 'error');

    let inner = '';

    if (req) {
        inner += ollamaCollapsible('ollama-request', 'request', model, _ollamaJsonPre(req.payload), false);
    }

    responses.forEach(r => {
        inner += ollamaCollapsible('ollama-response', 'response', _ollamaResponseSnippet(r.payload), _ollamaJsonPre(r.payload), false);
    });

    if (streams.length) {
        const recon = _ollamaReconstructStream(streams);
        let body = `<div class="ollama-stream-text">${escapeHtml(recon.content || '(no content)')}</div>`;
        if (recon.thinking) {
            body += `<details class="ollama-substream"><summary>thinking</summary>` +
                    `<div class="ollama-stream-text">${escapeHtml(recon.thinking)}</div></details>`;
        }
        const sub = `${streams.length} chunk${streams.length !== 1 ? 's' : ''}`;
        inner += ollamaCollapsible('ollama-stream', 'stream', sub, body, false);
    }

    errors.forEach(e => {
        inner += ollamaCollapsible('ollama-error', 'error', '', _ollamaJsonPre(e.payload), true);
    });

    return `
        <div class="ollama-log-group">
            <div class="ollama-group-header">
                <span class="ollama-timestamp">${ts}</span>
                <span class="ollama-session">${escapeHtml(sessionShort)}</span>
            </div>
            <div class="ollama-group-body">${inner}</div>
        </div>
    `;
}

// One collapsible row: colored direction badge + optional sub-label in the
// summary, full content revealed on expand. `open` controls default state.
function ollamaCollapsible(kindClass, badge, sub, bodyHtml, open) {
    const subHtml = sub ? `<span class="ollama-sub">${escapeHtml(String(sub))}</span>` : '';
    return `<details class="ollama-item ${kindClass}"${open ? ' open' : ''}>
                <summary>
                    <span class="ollama-direction">${badge}</span>
                    ${subHtml}
                </summary>
                <div class="ollama-item-body">${bodyHtml}</div>
            </details>`;
}

function _ollamaJsonPre(payload) {
    let s;
    try { s = JSON.stringify(payload, null, 2); }
    catch (e) { s = String(payload); }
    return `<pre><code>${escapeHtml(s)}</code></pre>`;
}

// Concatenate streamed content/thinking across all chunks of a group.
function _ollamaReconstructStream(streams) {
    let content = '', thinking = '';
    for (const s of streams) {
        const p = s.payload || {};
        if (typeof p.content === 'string') content += p.content;
        if (typeof p.thinking === 'string') thinking += p.thinking;
    }
    return { content, thinking };
}

// Short one-line preview for a response summary.
function _ollamaResponseSnippet(payload) {
    if (!payload || typeof payload !== 'object') return '';
    if (typeof payload.content === 'string' && payload.content.trim()) {
        return payload.content.trim().slice(0, 60);
    }
    if (payload.type === 'tool_call' && payload.name) return 'tool: ' + payload.name;
    if (payload.name) return 'tool: ' + payload.name;
    return '';
}

async function clearLogs() {
    // Read the dropdown directly — the `currentLogType` variable can drift out
    // of sync (it's only set by the dropdown's onchange, not by tab switches),
    // which previously made this button silently refuse to clear.
    const logTypeSelect = document.getElementById('log-type-select');
    const selectedLogType = logTypeSelect ? logTypeSelect.value : 'agent';

    if (selectedLogType === 'agent') {
        // Don't clear agent logs from this UI
        showNotification('Switch to "Ollama API Logs" to clear them, or use Clear cache for agent logs', 'info');
        return;
    }

    if (!confirm('Clear all Ollama API logs?')) return;

    try {
        const response = await fetch('/api/ollama/logs/clear', withAuth({
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        }));

        if (!response.ok) {
            throw new Error('Failed to clear logs');
        }

        // Reset the render cache so the now-empty state actually paints
        // (loadOllamaLogs skips re-rendering when the markup is unchanged).
        _lastLogsHtml = '';
        const sessionSelect = document.getElementById('log-session-select');
        if (sessionSelect) {
            sessionSelect.dataset.optionsKey = '';
            sessionSelect.value = '';
        }
        await loadOllamaLogs('');
        showNotification('Ollama API logs cleared', 'success');
    } catch (error) {
        showNotification(`Error clearing logs: ${error.message}`, 'error');
    }
}
