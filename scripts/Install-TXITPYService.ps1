#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$ServiceName = "TXITPY",
    [string]$AppDirectory = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath,
    [string]$NssmPath,
    [string]$ConfigFile,
    [string]$TokenFile = (Join-Path $env:ProgramData "TXITPY\discord-token.txt"),
    [string]$ServiceAccount = "LocalSystem",
    [switch]$SkipStart
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-Executable {
    param(
        [string]$RequestedPath,
        [string]$CommandName,
        [string]$DisplayName
    )

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        if (-not (Test-Path -LiteralPath $RequestedPath -PathType Leaf)) {
            throw "$DisplayName was not found: $RequestedPath"
        }
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }

    $command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $command -or [string]::IsNullOrWhiteSpace($command.Source)) {
        throw "$DisplayName was not found. Install it, add it to PATH, or pass its full path."
    }
    return $command.Source
}

function Invoke-Nssm {
    param([Parameter(Mandatory)][string[]]$Arguments)

    & $script:NssmExecutable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "NSSM failed with exit code $LASTEXITCODE while running: $($Arguments -join ' ')"
    }
}

function Quote-ProcessArgument {
    param([Parameter(Mandatory)][string]$Value)

    if ($Value.Contains('"')) {
        throw "A path or argument contains an unsupported quotation mark: $Value"
    }
    return '"' + $Value + '"'
}

function Set-ServiceAccount {
    if ([string]::IsNullOrWhiteSpace($ServiceAccount) -or $ServiceAccount -eq "LocalSystem") {
        Invoke-Nssm @("reset", $ServiceName, "ObjectName")
        return
    }

    $securePassword = Read-Host "Password for service account $ServiceAccount" -AsSecureString
    $passwordPointer = [IntPtr]::Zero
    $plainPassword = $null
    try {
        $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
        Invoke-Nssm @("set", $ServiceName, "ObjectName", $ServiceAccount, $plainPassword)
    }
    finally {
        if ($passwordPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        }
        $plainPassword = $null
        $securePassword.Dispose()
    }
}

$AppDirectory = (Resolve-Path -LiteralPath $AppDirectory).Path
$PythonExecutable = Resolve-Executable -RequestedPath $PythonPath -CommandName "python.exe" -DisplayName "Python"
$script:NssmExecutable = Resolve-Executable -RequestedPath $NssmPath -CommandName "nssm.exe" -DisplayName "NSSM"

$mainScript = Join-Path $AppDirectory "main.py"
if (-not (Test-Path -LiteralPath $mainScript -PathType Leaf)) {
    throw "main.py was not found in the application directory: $AppDirectory"
}

if ([string]::IsNullOrWhiteSpace($ConfigFile)) {
    if ([string]::IsNullOrWhiteSpace($env:APPDATA)) {
        throw "APPDATA is unavailable. Pass -ConfigFile with the TXITPY transmit-settings.json path."
    }
    $ConfigFile = Join-Path $env:APPDATA "TXITPY\transmit-settings.json"
}
$ConfigFile = [IO.Path]::GetFullPath($ConfigFile)
if (-not (Test-Path -LiteralPath $ConfigFile -PathType Leaf)) {
    throw "TXITPY settings were not found at $ConfigFile. Open TXITPY, save the transmit settings, or pass -ConfigFile."
}

$TokenFile = [IO.Path]::GetFullPath($TokenFile)
$tokenDirectory = Split-Path -Parent $TokenFile
$programDataDirectory = Join-Path $env:ProgramData "TXITPY"
$logDirectory = Join-Path $programDataDirectory "logs"

New-Item -ItemType Directory -Path $tokenDirectory -Force | Out-Null
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

if (-not (Test-Path -LiteralPath $TokenFile -PathType Leaf)) {
    New-Item -ItemType File -Path $TokenFile -Force | Out-Null
    Write-Host "Created an empty token file: $TokenFile" -ForegroundColor Yellow
    Write-Host "Put the current TXITY Discord session token in that file, then run this installer again."
    exit 1
}
if ([string]::IsNullOrWhiteSpace((Get-Content -LiteralPath $TokenFile -Raw -ErrorAction Stop))) {
    throw "The Discord token file is empty: $TokenFile. Add the current session token and run this installer again."
}

$serviceIdentity = if ($ServiceAccount -eq "LocalSystem") { "SYSTEM" } else { $ServiceAccount }

# Keep the session token readable only by the service identity and administrators.
$tokenGrants = @("SYSTEM:F", "BUILTIN\Administrators:F")
if ($serviceIdentity -ne "SYSTEM") {
    $tokenGrants += "$($serviceIdentity):R"
}
& icacls.exe $TokenFile /inheritance:r /grant:r @tokenGrants | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not restrict access to the token file. Check the file's permissions and try again."
}

# The service needs write access to its rotating log files.
& icacls.exe $logDirectory /grant "$($serviceIdentity):(OI)(CI)M" /T /C | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not grant the service account access to $logDirectory."
}
if ($ServiceAccount -ne "LocalSystem") {
    & icacls.exe $ConfigFile /grant "$($serviceIdentity):R" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not grant $ServiceAccount read access to $ConfigFile."
    }
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
$parametersKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters"
if ($null -eq $service) {
    Invoke-Nssm @("install", $ServiceName, $PythonExecutable)
}
elseif (-not (Test-Path -LiteralPath $parametersKey)) {
    throw "A Windows service named $ServiceName already exists and is not managed by NSSM."
}
else {
    if ($service.Status -ne "Stopped") {
        Stop-Service -Name $ServiceName -Force
        (Get-Service -Name $ServiceName).WaitForStatus(
            [System.ServiceProcess.ServiceControllerStatus]::Stopped,
            [TimeSpan]::FromSeconds(20)
        )
    }
}

$appParameters = @(
    (Quote-ProcessArgument $mainScript),
    "--no-gui",
    "--auth-token-file",
    (Quote-ProcessArgument $TokenFile),
    "--config",
    (Quote-ProcessArgument $ConfigFile),
    "--log-file",
    (Quote-ProcessArgument (Join-Path $logDirectory "txitpy.log"))
) -join " "

Invoke-Nssm @("set", $ServiceName, "Application", $PythonExecutable)
Invoke-Nssm @("set", $ServiceName, "AppDirectory", $AppDirectory)
Invoke-Nssm @("set", $ServiceName, "AppParameters", $appParameters)
Invoke-Nssm @("set", $ServiceName, "AppExit", "Default", "Restart")
Invoke-Nssm @("set", $ServiceName, "AppRestartDelay", "5000")
Invoke-Nssm @("set", $ServiceName, "AppStdout", (Join-Path $logDirectory "nssm-stdout.log"))
Invoke-Nssm @("set", $ServiceName, "AppStderr", (Join-Path $logDirectory "nssm-stderr.log"))
Invoke-Nssm @("set", $ServiceName, "AppRotate", "1")
Invoke-Nssm @("set", $ServiceName, "AppRotateOnline", "1")
Invoke-Nssm @("set", $ServiceName, "AppRotateBytes", "10485760")
Invoke-Nssm @("set", $ServiceName, "Start", "SERVICE_AUTO_START")
Set-ServiceAccount

if (-not $SkipStart) {
    Start-Service -Name $ServiceName
    (Get-Service -Name $ServiceName).WaitForStatus(
        [System.ServiceProcess.ServiceControllerStatus]::Running,
        [TimeSpan]::FromSeconds(20)
    )
    Write-Host "TXITPY service '$ServiceName' is running." -ForegroundColor Green
}
else {
    Write-Host "TXITPY service '$ServiceName' is installed and configured."
    Write-Host "Start it with: Start-Service -Name '$ServiceName'"
}

Write-Host "Application: $AppDirectory"
Write-Host "Python:      $PythonExecutable"
Write-Host "Settings:    $ConfigFile"
Write-Host "Token file:  $TokenFile"
Write-Host "Logs:        $logDirectory"
Write-Host "To remove the service, run scripts\Remove-TXITPYService.ps1."
