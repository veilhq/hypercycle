$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("$PSScriptRoot\Hypercycle.lnk")
$sc.TargetPath = (Get-Command pythonw).Source
$sc.Arguments = "$PSScriptRoot\hypercycle.py"
$sc.WorkingDirectory = "$PSScriptRoot"
$sc.IconLocation = "$PSScriptRoot\assets\icons\hypercycle.ico,0"
$sc.Save()
Write-Host "Shortcut created at: $PSScriptRoot\Hypercycle.lnk"
