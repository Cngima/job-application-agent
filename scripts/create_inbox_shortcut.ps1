# Creates the WhatsApp job inbox folder and a "Job Inbox" shortcut on the Windows desktop.
# Run from the repository root:  powershell -ExecutionPolicy Bypass -File scripts\create_inbox_shortcut.ps1
$repo  = (Resolve-Path "$PSScriptRoot\..").Path
$inbox = Join-Path $repo "inbox"
New-Item -ItemType Directory -Force -Path (Join-Path $inbox "processed") | Out-Null
$desktop = [Environment]::GetFolderPath("Desktop")
$link = Join-Path $desktop "Job Inbox.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $inbox
$shortcut.Description = "Save WhatsApp job posts here as .txt files for job-application-agent"
$shortcut.IconLocation = "$env:SystemRoot\System32\shell32.dll,160"
$shortcut.Save()
Write-Output "Inbox folder: $inbox"
Write-Output "Shortcut:     $link"
