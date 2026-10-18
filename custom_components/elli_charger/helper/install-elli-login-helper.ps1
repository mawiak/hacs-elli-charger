<#
Elli Charger for Home Assistant - Windows login helper

Elli's login only redirects to its own mobile-app address
(com.elli.ios.emsp://...), which a desktop browser cannot open. This helper
registers that address for the current Windows user. When the browser hands
it over after the Elli login, the helper forwards it to Home Assistant, which
then completes the setup automatically - no copy & paste needed.

What it does (current user only, no admin rights needed):
  * writes %LOCALAPPDATA%\ElliLoginHelper\elli-callback.ps1
  * registers HKCU\Software\Classes\com.elli.ios.emsp
The forwarded address is only accepted by Home Assistant while a login you
started there is waiting for it.

Install:    right-click this file -> "Run with PowerShell"
            (or: powershell -ExecutionPolicy Bypass -File install-elli-login-helper.ps1)
Uninstall:  powershell -ExecutionPolicy Bypass -File install-elli-login-helper.ps1 -Uninstall
#>
param(
    [string]$HomeAssistantUrl = '@@HOME_ASSISTANT_URL@@',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$Scheme = 'com.elli.ios.emsp'
$InstallDir = Join-Path $env:LOCALAPPDATA 'ElliLoginHelper'
$HandlerPath = Join-Path $InstallDir 'elli-callback.ps1'
$KeyPath = "HKCU:\Software\Classes\$Scheme"

function Finish([string]$Message) {
    Write-Host ''
    Write-Host $Message
    Write-Host ''
    if ($Host.Name -eq 'ConsoleHost') { Read-Host 'Press Enter to close / Enter zum Schliessen' | Out-Null }
}

if ($Uninstall) {
    Remove-Item -Path $KeyPath -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    Finish 'Elli login helper removed. / Elli-Login-Helfer entfernt.'
    return
}

if (-not $HomeAssistantUrl -or $HomeAssistantUrl -match '^@@') {
    $HomeAssistantUrl = Read-Host 'Home Assistant URL (e.g. / z.B. http://192.168.1.10:8123)'
}
$HomeAssistantUrl = $HomeAssistantUrl.Trim().TrimEnd('/')
if ($HomeAssistantUrl -notmatch '^https?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~/-]*)?$') {
    throw "Invalid Home Assistant URL: $HomeAssistantUrl"
}

# The handler only accepts a well-formed Elli callback (no quotes/spaces) and
# never evaluates it - it is URL-encoded and opened as a normal web address.
$handler = @'
param([string]$CallbackUrl)
$HomeAssistantUrl = '__HA_URL__'
if (-not $CallbackUrl -or $CallbackUrl.Length -gt 4096 -or
    $CallbackUrl -notmatch '^com\.elli\.ios\.emsp://[A-Za-z0-9._~:/?#@!$&()*+,;=%\[\]-]+$') {
    exit 1
}
Start-Process ($HomeAssistantUrl + '/api/elli_charger/callback?url=' + [uri]::EscapeDataString($CallbackUrl))
'@
$handler = $handler.Replace('__HA_URL__', $HomeAssistantUrl)

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Set-Content -Path $HandlerPath -Value $handler -Encoding UTF8

$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$command = "`"$powershell`" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$HandlerPath`" `"%1`""

New-Item -Path $KeyPath -Force | Out-Null
Set-Item -Path $KeyPath -Value 'URL:Elli login for Home Assistant'
New-ItemProperty -Path $KeyPath -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
New-Item -Path "$KeyPath\shell\open\command" -Force | Out-Null
Set-Item -Path "$KeyPath\shell\open\command" -Value $command

Finish "Elli login helper installed for $HomeAssistantUrl.`nElli-Login-Helfer fuer $HomeAssistantUrl installiert.`n`nNext: in Home Assistant choose 'Log in automatically'. When the browser asks to open the link, allow it.`nWeiter: In Home Assistant 'Automatisch anmelden' waehlen. Wenn der Browser fragt, ob der Link geoeffnet werden soll, zulassen."
