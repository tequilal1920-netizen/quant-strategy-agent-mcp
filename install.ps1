param(
    [Parameter(Mandatory = $true)][string]$ServerUrl,
    [string]$Python = "",
    [string]$InstallDir = "$env:LOCALAPPDATA\QuantAgentClient",
    [switch]$WebOnly,
    [switch]$SkipCodexRegistration,
    [switch]$SkipActivation,
    [switch]$ManualStart,
    [switch]$NoPythonDownload
)
$ErrorActionPreference = "Stop"
$Source = Join-Path $PSScriptRoot "quant_agent_mcp"
if (-not (Test-Path -LiteralPath (Join-Path $Source "pyproject.toml"))) {
    throw "Use the owner's complete public client release. No private repository is required."
}
if ($WebOnly) { $SkipActivation = $true; $SkipCodexRegistration = $true }
$ResolvedInstall = [System.IO.Path]::GetFullPath($InstallDir)
if ($ResolvedInstall.Contains('"')) { throw "The installation path cannot contain a double quote." }
$EnvironmentDir = Join-Path $ResolvedInstall "python"
$ClientPython = Join-Path $EnvironmentDir "Scripts\python.exe"
$TempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\') + '\'
$Stage = Join-Path $TempRoot ("quant-agent-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Stage | Out-Null

function Test-ClientPython([string]$Command) {
    if (-not $Command) { return $null }
    $Entry = Get-Command $Command -ErrorAction SilentlyContinue
    if (-not $Entry -or $Entry.CommandType -ne "Application" -or $Entry.Source -match '\\WindowsApps\\') { return $null }
    $ProbeInfo = New-Object System.Diagnostics.ProcessStartInfo
    $ProbeInfo.FileName = $Entry.Source
    $ProbeInfo.Arguments = '-I -B -c "import sys; assert sys.version_info >= (3,11); print(sys.executable)"'
    $ProbeInfo.UseShellExecute = $false
    $ProbeInfo.CreateNoWindow = $true
    $ProbeInfo.RedirectStandardOutput = $true
    $ProbeInfo.RedirectStandardError = $true
    $Probe = [System.Diagnostics.Process]::Start($ProbeInfo)
    try {
        if (-not $Probe.WaitForExit(15000)) { $Probe.Kill(); $Probe.WaitForExit(); return $null }
        $Result = $Probe.StandardOutput.ReadToEnd().Trim()
        if ($Probe.ExitCode -eq 0 -and $Result -and (Test-Path -LiteralPath $Result)) { return $Result }
        return $null
    } finally { $Probe.Dispose() }
}

try {
    $SelectedPython = Test-ClientPython $ClientPython
    if (-not $SelectedPython) { $SelectedPython = Test-ClientPython $Python }
    if (-not $SelectedPython -and $Python) { throw "The supplied -Python executable must be Python 3.11 or newer." }
    foreach ($Name in @("python3.exe", "python.exe")) {
        if (-not $SelectedPython) { $SelectedPython = Test-ClientPython $Name }
    }
    if (-not $SelectedPython) {
        if ($NoPythonDownload) {
            throw "Install Python 3.11+ from https://www.python.org/downloads/windows/ and rerun with -Python its full python.exe path."
        }
        # Fixed official release and digests, never execute an unverified URL or a mutable latest installer.
        $Version = "3.13.15"
        $Architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        $Artifacts = @{
            "AMD64" = @("amd64", "edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403")
            "ARM64" = @("arm64", "c252c676087c49e6b94e95a273536b78921c28a5fc9f86d15d25392328247249")
            "x86" = @("", "741c07276eb2d57e7ee012d643f021c58cb38d11c5389be46c15d41d1a10b447")
        }
        if (-not $Artifacts.ContainsKey($Architecture)) { throw "Unsupported Windows architecture; supply -Python with Python 3.11+." }
        $Artifact = $Artifacts[$Architecture]
        $Suffix = if ($Artifact[0]) { "-" + $Artifact[0] } else { "" }
        $Installer = Join-Path $Stage ("python-" + $Version + $Suffix + ".exe")
        $Url = "https://www.python.org/ftp/python/$Version/python-$Version$Suffix.exe"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Write-Host "Python is missing. Downloading verified Python $Version from python.org for this user."
        Invoke-WebRequest -Uri $Url -OutFile $Installer -UseBasicParsing -MaximumRedirection 0
        if ((Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Artifact[1]) { throw "Python installer SHA256 mismatch; nothing was executed." }
        $Signature = Get-AuthenticodeSignature -LiteralPath $Installer
        if ($Signature.Status -ne "Valid" -or $Signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            throw "Python installer signature verification failed; nothing was executed."
        }
        $RuntimeDir = Join-Path $ResolvedInstall "runtime"
        $Arguments = @("/quiet", "InstallAllUsers=0", 'TargetDir="' + $RuntimeDir + '"',
            "PrependPath=0", "Include_launcher=0", "Include_test=0", "Include_doc=0", "AssociateFiles=0", "Shortcuts=0")
        $InstallProcess = Start-Process -FilePath $Installer -ArgumentList $Arguments -Wait -PassThru -WindowStyle Hidden
        if ($InstallProcess.ExitCode -notin @(0, 3010)) { throw "Python installation failed. Install Python from python.org and rerun with -Python." }
        $SelectedPython = Test-ClientPython (Join-Path $RuntimeDir "python.exe")
        if (-not $SelectedPython) { throw "The downloaded Python did not pass its startup check." }
    }
    if (-not (Test-Path -LiteralPath $ClientPython)) {
        & $SelectedPython -I -B -m venv $EnvironmentDir
        if ($LASTEXITCODE -ne 0) { throw "Could not create the client environment." }
    }
    # Build only in system Temp; pip/setuptools must not create egg-info in the release/project folder.
    $BuildSource = Join-Path $Stage "package"
    $BuildPackage = Join-Path $BuildSource "quant_agent_mcp"
    New-Item -ItemType Directory -Path $BuildPackage -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $Source "pyproject.toml") -Destination $BuildSource
    foreach ($Name in @("__init__.py", "config.py", "credentials.py", "client.py", "server.py", "device_bridge.py", "cli.py")) {
        Copy-Item -LiteralPath (Join-Path (Join-Path $Source "quant_agent_mcp") $Name) -Destination $BuildPackage
    }
    & $ClientPython -I -B -m pip install --disable-pip-version-check --no-cache-dir --constraint (Join-Path $PSScriptRoot "requirements-windows.lock") $BuildSource
    if ($LASTEXITCODE -ne 0) { throw "Client dependency installation failed." }
    & $ClientPython -I -B -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Client dependencies are inconsistent." }
    $ConfigureArguments = @("-I", "-B", "-m", "quant_agent_mcp.cli", "configure", "--server", $ServerUrl, "--start")
    if (-not $ManualStart) { $ConfigureArguments += "--start-on-login" }
    & $ClientPython @ConfigureArguments
    if ($LASTEXITCODE -ne 0) { throw "Client configuration or device health verification failed." }
    if (-not $SkipActivation) {
        & $ClientPython -I -B -m quant_agent_mcp.cli activate
        if ($LASTEXITCODE -ne 0) { throw "MCP invitation activation failed. The web slot remains separate." }
    }
    if (-not $SkipCodexRegistration) {
        & $ClientPython -I -B -m quant_agent_mcp.cli register-codex
        if ($LASTEXITCODE -ne 0) { Write-Warning "Automatic Codex registration is incomplete. Add the STDIO command shown below in Codex Settings > MCP servers." }
    }
    & $ClientPython -I -B -m quant_agent_mcp.cli status
    if ($LASTEXITCODE -ne 0) { throw "Final device health verification failed." }
    if ($WebOnly) {
        Write-Host "Web device component is ready. Enter your invitation on the configured website; the MCP slot was not activated."
    } else {
        Write-Host "STDIO command: $ClientPython"
        Write-Host "Arguments: -I -B -m quant_agent_mcp.server"
        Write-Host "Restart the quant-agent MCP connection in Codex after registration."
    }
    if ($ManualStart) { Write-Warning "Automatic login startup was skipped. Run the client start-device command after each OS login." }
} finally {
    $ResolvedStage = [System.IO.Path]::GetFullPath($Stage)
    if ($ResolvedStage.StartsWith($TempRoot, [StringComparison]::OrdinalIgnoreCase) -and
        [System.IO.Path]::GetFileName($ResolvedStage) -match '^quant-agent-install-[a-f0-9]{32}$') {
        Remove-Item -LiteralPath $ResolvedStage -Recurse -Force -ErrorAction SilentlyContinue
    }
}
