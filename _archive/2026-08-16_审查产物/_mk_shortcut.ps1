$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("C:\Users\Administrator\Desktop\Command Code.lnk")
$sc.TargetPath = "C:\Users\Administrator\AppData\Roaming\npm\cmdc.cmd"
$sc.WorkingDirectory = "D:\work"
$sc.Description = "Command Code AI Coding Agent"
$sc.Save()
Write-Host "Shortcut created on desktop"