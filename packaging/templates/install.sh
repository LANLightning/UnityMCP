#!/usr/bin/env bash
# unity-mcp-py 部署安装 (macOS/Linux)。详见 DEPLOY.md。
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3 (需 >= 3.11)。装好 Python 后再运行。" >&2
  exit 1
fi

wheel="$(ls "$here"/unity_mcp_py-*.whl 2>/dev/null | head -n1 || true)"
if [ -z "$wheel" ]; then
  echo "本目录未找到 wheel (unity_mcp_py-*.whl)。" >&2
  exit 1
fi

echo "安装 $(basename "$wheel") ..."
python3 -m pip install --upgrade "$wheel"

cat <<'EOF'

[OK] Python 侧已安装。验证: unity-mcp-py --version

下一步 (手动):
 1. Unity: Package Manager -> Add package from tarball -> 选本目录的 com.local.unitymcp-bridge-*.tgz
 2. 把 claude.json.snippet 填好后并入 ~/.claude.json 的 mcpServers
 3. 把 .env.example 的占位填好 (或在 env 块里直接设 UNITY_MCP_PROJECT_PATH)
 4. 验证桥: nc -zv 127.0.0.1 17873
EOF
