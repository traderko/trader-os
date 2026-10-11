# util/win_task.ps1 - used by start.bat (keep ASCII-only)
#
# Registers a Windows Task Scheduler task "TraderOS" that runs start.bat with highest privileges,
# so later launches start as administrator without the UAC prompt.
#   check    : exit 0 if the task exists and points to this start.bat
#   run      : start the task (exit 1 if missing or it points to another folder)
#   register : create/update the task (needs administrator) - no trigger, it only runs when start.bat asks
#   remove   : delete the task
#
# The task: interactive (window visible), one instance at a time, no time limit
# (Task Scheduler stops tasks after 72 hours by default - this one must keep running).

param([string]$Mode, [string]$Bat)
$ErrorActionPreference = 'Stop'
$Name = 'TraderOS'

function Test-Ours {
    try { $t = Get-ScheduledTask -TaskName $Name -ErrorAction Stop } catch { return $false }
    $a = $t.Actions | Select-Object -First 1
    if (-not $a -or -not $a.Arguments) { return $false }
    return $a.Arguments.ToLower().Contains($Bat.ToLower())
}

try {
    switch ($Mode) {
        'check' { if (Test-Ours) { exit 0 } else { exit 1 } }
        'run' {
            if (-not (Test-Ours)) { exit 1 }
            Start-ScheduledTask -TaskName $Name
            exit 0
        }
        'register' {
            if (Test-Ours) { exit 0 }
            $dir = Split-Path -Parent $Bat
            $cmd = Join-Path $env:SystemRoot 'System32\cmd.exe'
            $act = New-ScheduledTaskAction -Execute $cmd -Argument "/c `"`"$Bat`"`"" -WorkingDirectory $dir
            $pri = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
            $set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
                       -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd
            Register-ScheduledTask -TaskName $Name -Action $act -Principal $pri -Settings $set -Force `
                -Description 'TraderOS server - start.bat as administrator without the UAC prompt' | Out-Null
            exit 0
        }
        'remove' {
            Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
            exit 0
        }
    }
} catch {
    Write-Host "  [task] $($_.Exception.Message)"
    exit 1
}
exit 2
