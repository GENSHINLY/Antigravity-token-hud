$ErrorActionPreference = "Stop"

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$csFile = Join-Path $scriptDir "hud_launcher.cs"
$scriptPy = Join-Path $scriptDir "token_hud.py"
$localExe = Join-Path $scriptDir "hud.exe"

$winDir = [Environment]::GetFolderPath("Windows")
$cscCandidates = @(
    (Join-Path $winDir "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $winDir "Microsoft.NET\Framework\v4.0.30319\csc.exe")
)

$cscPath = $null
foreach ($candidate in $cscCandidates) {
    if (Test-Path $candidate) {
        $cscPath = $candidate
        break
    }
}

if (-not $cscPath) {
    $cmd = Get-Command "csc.exe" -ErrorAction SilentlyContinue
    if ($cmd) {
        $cscPath = $cmd.Source
    }
}

if (-not $cscPath) {
    throw "未找到 C# 编译器 (csc.exe)"
}

& $cscPath /nologo /target:winexe /optimize+ "/out:$localExe" "$csFile"
if ($LASTEXITCODE -ne 0) {
    throw "编译 hud.exe 失败，退出码: $LASTEXITCODE"
}

Write-Output "Built successfully: $localExe"

if ($env:USERPROFILE) {
    $userBinDir = Join-Path $env:USERPROFILE ".local\bin"
    $userBinExe = Join-Path $userBinDir "hud.exe"
    $userBinPathTxt = Join-Path $userBinDir "token_hud_path.txt"

    if (-not (Test-Path $userBinDir)) {
        New-Item -ItemType Directory -Path $userBinDir -Force | Out-Null
    }

    Copy-Item -Path $localExe -Destination $userBinExe -Force
    Set-Content -Path $userBinPathTxt -Value $scriptPy -Encoding UTF8 -NoNewline
    Write-Output "Synced launcher to: $userBinExe"
}
