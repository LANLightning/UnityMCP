# Re-sync vendored copies of bridge_client.py and human_collab.py from the
# ECC flow install at ~/.claude/flow/lib. Run this after upstream flow changes,
# then re-build the wheel.
#
# Usage:
#   pwsh -File scripts\sync_from_flow.ps1
#   pwsh -File scripts\sync_from_flow.ps1 -FlowDir "<FLOW_ROOT>"

param(
    [string]$FlowDir = (Join-Path $env:USERPROFILE ".claude\flow")
)

$ErrorActionPreference = "Stop"

$flowLib = Join-Path $FlowDir "lib"
$repoRoot = Split-Path -Parent $PSScriptRoot
$vendorDir = Join-Path $repoRoot "src\unity_mcp_py\_vendor"

foreach ($f in @("bridge_client.py", "human_collab.py")) {
    $srcPath = Join-Path $flowLib $f
    if (-not (Test-Path $srcPath)) {
        throw "Source file not found: $srcPath"
    }
}

Copy-Item (Join-Path $flowLib "bridge_client.py") (Join-Path $vendorDir "bridge_client.py") -Force
Copy-Item (Join-Path $flowLib "human_collab.py")  (Join-Path $vendorDir "human_collab.py")  -Force

# Rewrite the only intra-package import inside the vendored human_collab.py
$humanCollabPath = Join-Path $vendorDir "human_collab.py"
(Get-Content $humanCollabPath -Raw) `
    -replace 'from lib\.bridge_client', 'from .bridge_client' |
    Set-Content $humanCollabPath -NoNewline

# --- H1 self-heal: neutralize the hardcoded dev-machine schema path that the
# upstream flow copy of bridge_client.py still carries. Re-applied on every sync
# so the dev fingerprint never re-enters the shipped wheel. Idempotent: the
# repo copy already uses the env-var form, so re-running is a no-op.
# See docs/specs/2026-06-09-deployment-bundle-design.md §3 H1.
$bcPath = Join-Path $vendorDir "bridge_client.py"
# Read/write as UTF-8 (no BOM). PS 5.1's Get-Content/Set-Content default to the
# ANSI codepage, which mangles the file's UTF-8 em-dashes on every round-trip;
# use [System.IO.File] with an explicit no-BOM UTF8 encoding to keep bytes intact.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$bc = [System.IO.File]::ReadAllText($bcPath, [System.Text.Encoding]::UTF8)

# 1. Rewrite `project_fixture = Path( "<devpath>" "<suffix>" )` (any whitespace/newlines) into
#    an env-var read. Singleline so '.' spans the two adjacent string literals.
$bc = [System.Text.RegularExpressions.Regex]::Replace(
    $bc,
    'project_fixture = Path\(\s*".*?protocol\.schema\.json"\s*\)',
    'env_schema = Path(os.environ.get("UNITY_MCP_SCHEMA_PATH", "")) if os.environ.get("UNITY_MCP_SCHEMA_PATH") else None',
    [System.Text.RegularExpressions.RegexOptions]::Singleline
)
# 2. Repoint the branch that consumed `project_fixture`.
$bc = $bc -replace 'elif project_fixture\.exists\(\):', 'elif env_schema is not None and env_schema.exists():'
$bc = $bc -replace 'self\._schema_path = project_fixture\b', 'self._schema_path = env_schema'

[System.IO.File]::WriteAllText($bcPath, $bc, $utf8NoBom)

# Guard: fail loudly if any dev fingerprint survived the rewrite.
if ($bc -match 'EXAMPLE_DEVELOPER_IDENTIFIER|EXAMPLE_MACHINE_PREFIX') {
    throw "H1 neutralization failed: dev fingerprint still in $bcPath. Re-apply the UNITY_MCP_SCHEMA_PATH rewrite (docs/specs/2026-06-09-deployment-bundle-design.md §3 H1) before building."
}
Write-Host "  -> neutralized schema path to UNITY_MCP_SCHEMA_PATH in $bcPath"

Write-Host "Synced from $flowLib"
Write-Host "  -> $vendorDir\bridge_client.py"
Write-Host "  -> $vendorDir\human_collab.py (with import rewrite)"
Write-Host ""
Write-Host "Next: run tests, then 'python -m build --wheel'"
