# unity-mcp-py 部署安装 (Windows)。详见 DEPLOY.md。
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 python (需 >= 3.11)。装好 Python 后再运行。"
    exit 1
}

$wheel = Get-ChildItem -Path $here -Filter "unity_mcp_py-*.whl" | Select-Object -First 1
if (-not $wheel) {
    Write-Error "本目录未找到 wheel (unity_mcp_py-*.whl)。"
    exit 1
}

Write-Host "安装 $($wheel.Name) ..."
python -m pip install --upgrade $wheel.FullName
if ($LASTEXITCODE -ne 0) { Write-Error "pip install 失败"; exit 1 }

Write-Host ""
Write-Host "[OK] Python 侧已安装。验证: unity-mcp-py --version"
Write-Host ""
Write-Host "下一步 (手动):"
Write-Host " 1. Unity: Package Manager -> Add package from tarball -> 选本目录的 com.local.unitymcp-bridge-*.tgz"
Write-Host " 2. 把 claude.json.snippet 填好后并入 ~/.claude.json 的 mcpServers"
Write-Host " 3. 把 .env.example 的占位填好 (或在上面的 env 块里直接设 UNITY_MCP_PROJECT_PATH)"
Write-Host " 4. 验证桥: Test-NetConnection 127.0.0.1 -Port 17873"
