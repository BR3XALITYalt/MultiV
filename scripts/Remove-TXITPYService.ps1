#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$ServiceName = "TXITPY",
    [string]$NssmPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($NssmPath)) {
    $command = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if ($null -eq $command -or [string]::IsNullOrWhiteSpace($command.Source)) {
        throw "NSSM was not found. Add nssm.exe to PATH or pass -NssmPath."
    }
    $NssmPath = $command.Source
}
elseif (-not (Test-Path -LiteralPath $NssmPath -PathType Leaf)) {
    throw "NSSM was not found: $NssmPath"
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($null -eq $service) {
    Write-Host "Service '$ServiceName' is not installed."
    exit 0
}

$parametersKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
if (-not (Test-Path -LiteralPath $parametersKey)) {
    throw "Service '$ServiceName' is not managed by NSSM. It was left unchanged."
}

if ($service.Status -ne "Stopped") {
    Stop-Service -Name $ServiceName -Force
    (Get-Service -Name $ServiceName).WaitForStatus(
        [System.ServiceProcess.ServiceControllerStatus]::Stopped,
        [TimeSpan]::FromSeconds(20)
    )
}

& $NssmPath remove $ServiceName confirm
if ($LASTEXITCODE -ne 0) {
    throw "NSSM failed to remove service '$ServiceName' (exit code $LASTEXITCODE)."
}

Write-Host "Removed service '$ServiceName'."
Write-Host "TXITPY settings, token file, and logs were left in place."
