param([switch]$Undo)

$ErrorActionPreference = 'Stop'
$relayExe = Join-Path $PSScriptRoot 'bun.exe'
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Administrator permission is required to change Windows Firewall.'
    Write-Host 'Right-click repair-pairing.cmd and choose Run as administrator.'
    exit 1
}
if (-not (Test-Path -LiteralPath $relayExe -PathType Leaf)) {
    Write-Host 'Keep this script beside the application bun.exe and try again.'
    exit 1
}
$relayExe = (Resolve-Path -LiteralPath $relayExe).Path
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($relayExe.ToLowerInvariant()))
    $suffix = ([BitConverter]::ToString($digest)).Replace('-', '').Substring(0, 12)
} finally {
    $sha.Dispose()
}
$ruleName = "CoyoteInCradle-Relay-9998-LAN-$suffix"
$existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($Undo) {
    if ($existing) { $existing | Remove-NetFirewallRule }
    Write-Host 'Removed only the relay rule created by this script.'
    exit 0
}
if ($existing) {
    $existing | Remove-NetFirewallRule
}
New-NetFirewallRule -Name $ruleName `
    -DisplayName 'Coyote in Cradle - relay 9998 (local subnet only)' `
    -Description 'DG-LAB Socket V4 pairing: allow local subnet access to this application relay only.' `
    -Direction Inbound -Action Allow -Enabled True -Profile Private,Public `
    -Program $relayExe -Protocol TCP -LocalPort 9998 `
    -RemoteAddress LocalSubnet -EdgeTraversalPolicy Block | Out-Null
Write-Host 'Firewall rule installed: TCP 9998, this bun.exe, local subnet only.'
Write-Host 'Restart Coyote in Cradle, then scan the NEW QR code in DG-LAB 4 Socket V4.'
Write-Host 'If pairing still fails, open http://COMPUTER-IP:9998/ on the phone.'
Write-Host 'WebSocket upgrade required means the phone can reach the relay.'
Write-Host 'A timeout can indicate Wi-Fi client isolation; try a different shared hotspot.'
Write-Host 'To undo: run repair-pairing.cmd -Undo as administrator.'
