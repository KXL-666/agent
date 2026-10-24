param(
    [string]$Python = "python",
    [switch]$SkipOffice
)

$ErrorActionPreference = "Stop"
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$taskBackend = Join-Path $taskRoot "backend"
& $Python -m pip install -r (Join-Path $taskBackend "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Python 依赖安装失败" }

$taskOffice = Join-Path $taskBackend "workspace\tools\libreoffice\program\soffice.com"
$taskHasOffice = & $Python -c "import sys; sys.path.insert(0, sys.argv[1]); import office_runtime; print('1' if office_runtime.find_office() else '0')" $taskBackend
if (-not $SkipOffice -and $taskHasOffice -ne "1") {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "未找到 winget。请安装 LibreOffice，并通过 OFFICE_SOFFICE 指定 soffice 路径。"
    }
    $taskDownload = Join-Path $taskBackend "workspace\tools\downloads"
    New-Item -ItemType Directory -Path $taskDownload -Force | Out-Null
    & winget download --id TheDocumentFoundation.LibreOffice --exact --source winget --accept-source-agreements --accept-package-agreements --skip-dependencies --download-directory $taskDownload
    if ($LASTEXITCODE -ne 0) { throw "LibreOffice 下载或哈希验证失败" }
    $taskInstaller = Get-ChildItem -LiteralPath $taskDownload -Filter "LibreOffice*.msi" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $taskInstaller) { throw "未找到已验证的 LibreOffice MSI 包" }
    $taskTarget = Join-Path $taskBackend "workspace\tools\libreoffice"
    $taskLog = Join-Path $taskBackend "workspace\tools\libreoffice-extract.log"
    $taskProcess = Start-Process -FilePath "msiexec.exe" -ArgumentList @(
        "/a", ('"' + $taskInstaller.FullName + '"'), "/qn",
        ('TARGETDIR="' + $taskTarget + '"'), "/l*v", ('"' + $taskLog + '"')
    ) -WindowStyle Hidden -Wait -PassThru
    if ($taskProcess.ExitCode -ne 0) { throw "LibreOffice 解包失败，请查看 $taskLog" }
}

Push-Location -LiteralPath $taskBackend
try {
    & $Python -c "import json, office_runtime; print(json.dumps(office_runtime.capabilities(), ensure_ascii=True))"
} finally {
    Pop-Location
}
Write-Output "依赖检查完成，请以上方能力检查结果为准。代码运行验收需要可用的 Docker 服务；未配置时会显示未验证。"
