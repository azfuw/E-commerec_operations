param(
    [int]$Port = 8010,
    [string]$Python = '',
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskRuntime = Join-Path $PSScriptRoot 'data\logistics-demo'
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$env:TEMP = $taskRuntime
$env:TMP = $taskRuntime
$env:PYTHONPYCACHEPREFIX = Join-Path $taskRuntime 'pycache'
$env:PYTHONIOENCODING = 'utf-8'
$env:NPM_CONFIG_CACHE = Join-Path $taskRuntime 'npm-cache'

if (-not $Python) {
    $taskCandidates = @('D:\E-commerce_operations_env\python.exe', (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'))
    foreach ($taskCandidate in $taskCandidates) {
        if (Test-Path -LiteralPath $taskCandidate) {
            & $taskCandidate -c 'import sys; assert sys.version_info[:2] == (3, 11); import fastapi, sqlalchemy, aiosqlite' 2>$null
            if ($LASTEXITCODE -eq 0) { $Python = $taskCandidate; break }
        }
    }
}
if (-not $Python) { throw '请通过 -Python 指定安装了项目依赖和 aiosqlite 的 Python 3.11 解释器。' }
if (-not $SkipBuild) {
    if (-not (Test-Path -LiteralPath 'frontend\node_modules')) {
        & npm.cmd --prefix frontend ci
        if ($LASTEXITCODE -ne 0) { throw '前端依赖安装失败。' }
    }
    & npm.cmd --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { throw '前端构建失败。' }
}
& $Python -m scripts.run_logistics_demo --port $Port
if ($LASTEXITCODE -ne 0) { throw '物流演示服务未能启动，请查看上方错误。' }
