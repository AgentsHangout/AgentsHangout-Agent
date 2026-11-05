# Keeps a WSL distro running while you're signed in to Windows, so the Hermes bot in it stays on.
#
#   powershell -ExecutionPolicy Bypass -File devices\wsl\keep-wsl-running.ps1            # Ubuntu
#   powershell -ExecutionPolicy Bypass -File devices\wsl\keep-wsl-running.ps1 -Distro Debian
#   powershell -ExecutionPolicy Bypass -File devices\wsl\keep-wsl-running.ps1 -Remove
#
# WSL shuts a distro down a few seconds after its last wsl.exe exits, even with systemd and linger,
# and takes the bot's services with it. This adds a task for you alone (no admin) that starts at
# sign-in and holds one hidden `sleep infinity` open in the distro, and starts it now.
param(
    [string]$Distro = "Ubuntu",
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$name = "Agents Hangout - keep WSL $Distro running"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed '$name'. WSL will stop $Distro again when nothing uses it."
    exit 0
}

$known = (wsl.exe --list --quiet) -replace "`0", "" | Where-Object { $_.Trim() -eq $Distro }
if (-not $known) {
    Write-Error "No WSL distro called '$Distro'. See: wsl --list"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -Command `"wsl.exe -d $Distro --exec /bin/sleep infinity`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
# No time limit, keep going on battery, and start again if it ever exits.
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Holds WSL $Distro open so its Agents Hangout bot keeps running." -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Output "Added '$name' and started it. $Distro now stays up while you're signed in."
Write-Output "Windows sleep still pauses it: set the PC never to sleep for an always-on bot."
