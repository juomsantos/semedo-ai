/**
 * dashboard.js — Real-time dashboard logic with 1.5 second polling
 */

const POLL_INTERVAL = 1500; // 1.5 seconds

let pollTimer = null;
let lastUpdate = new Date();

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

    // Modal close
    document.querySelector('.modal-close').addEventListener('click', closeModal);
    document.getElementById('task-modal').addEventListener('click', (e) => {
        if (e.target.id === 'task-modal') closeModal();
    });
}

// Start real-time polling
function startPolling() {
    console.log('Starting dashboard polling (1.5s interval)');
    
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
        
        // Add click handlers
        container.querySelectorAll('.task-item').forEach(el => {
            el.addEventListener('click', () => showApprovalDetail(el.dataset.taskId));
        });
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

// Show approval detail modal
async function showApprovalDetail(taskId) {
    try {
        const response = await fetch(`/api/pending-approvals/${taskId}`);
        const task = await response.json();
        
        const modal = document.getElementById('task-modal');
        const title = document.getElementById('modal-title');
        const body = document.getElementById('modal-body');
        
        title.textContent = `Task: ${task.id} (Awaiting Approval)`;
        
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
                    </div>
                </div>
                <div class="detail-section">
                    <h4>Task Description</h4>
                    <pre class="detail-result">${escapeHtml(task.body)}</pre>
                </div>
                <div class="detail-actions">
                    <button class="btn btn-success" onclick="approveTask('${task.id}')">Approve Task</button>
                    <button class="btn btn-danger" onclick="rejectTask('${task.id}')">Reject Task</button>
                </div>
            </div>
        `;
        
        body.innerHTML = html;
        modal.classList.add('show');
    } catch (error) {
        console.error('Error loading task detail:', error);
    }
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
        
        container.innerHTML = data.tasks.map(task => createTaskElement(task)).join('');
        
        // Add click handlers
        container.querySelectorAll('.task-item').forEach(el => {
            el.addEventListener('click', () => showTaskDetail(el.dataset.taskId));
        });
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
        
        container.innerHTML = data.tasks.map(task => createTaskElement(task)).join('');
        
        // Add click handlers
        container.querySelectorAll('.task-item').forEach(el => {
            el.addEventListener('click', () => showTaskDetail(el.dataset.taskId));
        });
    } catch (error) {
        console.error('Error updating history:', error);
    }
}

// Update agent statistics
async function updateAgentStats() {
    try {
        const response = await fetch('/api/agents');
        const stats = await response.json();
        
        const container = document.getElementById('agent-stats');
        const html = Object.entries(stats).map(([agent, data]) => `
            <div class="agent-stat">
                <div class="agent-name">${agent}</div>
                <div class="agent-metrics">
                    <span class="metric">✓ ${data.completed} completed</span>
                    <span class="metric error">✗ ${data.errors} errors</span>
                </div>
            </div>
        `).join('');
        
        container.innerHTML = html;
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

        container.innerHTML = data.logs.map(log => `
            <div class="log-entry">${escapeHtml(log)}</div>
        `).join('');

        // Auto-scroll to bottom
        container.parentElement.scrollTop = container.parentElement.scrollHeight;
    } catch (error) {
        console.error('Error updating logs:', error);
        const container = document.getElementById('logs-list');
        container.innerHTML = '<p class="no-data">Error loading logs: ' + error.message + '</p>';
    }
}

// Create task element HTML
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
        const response = await fetch(`/api/tasks/${taskId}`);
        const task = await response.json();
        
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
                    <pre class="detail-result">${escapeHtml(task.result.substring(0, 1000))}</pre>
                </div>
            `;
        }
        
        html += '</div>';
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
