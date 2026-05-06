# setup_scheduler.ps1 — Install Windows Task Scheduler entries for all agent scripts.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1
#
# This script creates scheduled tasks for each agent. Safe to re-run (removes old tasks first).

param(
    [string]$Action = "install"  # 'install' or 'remove'
)

$ErrorActionPreference = "Stop"

# Get the scripts directory
$ScriptsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptsDir
$Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
$LogsDir = Join-Path $ProjectRoot "logs"

Write-Host "Project root: $ProjectRoot"
Write-Host "Scripts dir:  $ScriptsDir"
Write-Host "Python:       $Python"
Write-Host "Logs dir:     $LogsDir"
Write-Host ""

# Verify python is available
if (-not $Python) {
    Write-Error "ERROR: python.exe not found in PATH"
    exit 1
}

# Verify we're running as admin
$admin = [bool]([System.Security.Principal.WindowsIdentity]::GetCurrent().Groups -match 'S-1-5-32-544')
if (-not $admin) {
    Write-Error "ERROR: This script must be run as Administrator"
    exit 1
}

# Task definitions
$tasks = @(
    @{
        Name = "AITeam-Orchestrator"
        Script = "agent_orchestrator.py"
        Interval = 1  # minutes
        Description = "AI Team Orchestrator - routes and decomposes tasks"
    }
    @{
        Name = "AITeam-Coder"
        Script = "agent_coder.py"
        Interval = 2
        Description = "AI Team Coder Worker - handles code generation tasks"
    }
    @{
        Name = "AITeam-Research"
        Script = "agent_research.py"
        Interval = 2
        Description = "AI Team Research Worker - handles research and summarization tasks"
    }
    @{
        Name = "AITeam-ClaudeCode"
        Script = "agent_claude_code.py"
        Interval = 3
        Description = "AI Team Claude Code Worker - handles complex tasks"
    }
)

function Remove-Tasks {
    Write-Host "Removing existing AI Team scheduled tasks..."
    foreach ($task in $tasks) {
        $taskPath = "\AITeam\$($task.Name)"
        try {
            Unregister-ScheduledTask -TaskPath "\AITeam\" -TaskName $task.Name -Confirm:$false -ErrorAction SilentlyContinue
            Write-Host "  Removed: $($task.Name)"
        } catch {
            # Task might not exist, that's fine
        }
    }
}

function Install-Tasks {
    Write-Host "Installing AI Team scheduled tasks..."

    # Create the folder for our tasks
    try {
        New-Item -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Schedule\TaskCache\Tree\AITeam" -Force -ErrorAction SilentlyContinue | Out-Null
    } catch { }

    foreach ($task in $tasks) {
        $scriptPath = Join-Path $ScriptsDir $task.Script
        $logPath = Join-Path $LogsDir ($task.Script -replace '\.py', '') "scheduler.log"

        # Ensure log directory exists
        $logDir = Split-Path -Parent $logPath
        New-Item -ItemType Directory -Path $logDir -Force -ErrorAction SilentlyContinue | Out-Null

        # Create the task action
        $action = New-ScheduledTaskAction `
            -Execute $Python `
            -Argument "`"$scriptPath`" >> `"$logPath`" 2>&1" `
            -WorkingDirectory $ScriptsDir

        # Create the task trigger (repeat every N minutes)
        $trigger = New-ScheduledTaskTrigger `
            -RepetitionInterval (New-TimeSpan -Minutes $task.Interval) `
            -RepetitionDuration (New-TimeSpan -Days 7305)  # ~20 years

        # Create the task settings
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -RunOnlyIfNetworkAvailable `
            -MultipleInstances IgnoreNew

        # Register the task
        try {
            Register-ScheduledTask `
                -TaskName $task.Name `
                -TaskPath "\AITeam\" `
                -Action $action `
                -Trigger $trigger `
                -Settings $settings `
                -Description $task.Description `
                -User "SYSTEM" `
                -Force `
                -ErrorAction Stop | Out-Null

            Write-Host "  Installed: $($task.Name) (every $($task.Interval) minutes)"
        } catch {
            Write-Error "Failed to install $($task.Name): $_"
        }
    }
}

# Main logic
if ($Action -eq "remove") {
    Remove-Tasks
    Write-Host ""
    Write-Host "AI Team scheduled tasks removed."
} else {
    Remove-Tasks
    Install-Tasks

    Write-Host ""
    Write-Host "Scheduled tasks installed:"
    Get-ScheduledTask -TaskPath "\AITeam\" | Select-Object -Property TaskName, State | Format-Table -AutoSize

    Write-Host ""
    Write-Host "Agent polling will start on the next scheduled interval."
    Write-Host "To verify: Get-ScheduledTask -TaskPath '\AITeam\' | Select-Object TaskName, NextRunTime"
    Write-Host "To watch logs: Get-Content -Path '$LogsDir\orchestrator\scheduler.log' -Wait"
}
