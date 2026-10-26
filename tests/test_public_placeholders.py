from pathlib import Path
root=Path(__file__).resolve().parent.parent

def test_readme_uses_public_adapter_placeholders():
    text=(root/'README.md').read_text(encoding='utf8')
    for name in ('<PROJECT_ROOT>', '<MCP_ROOT>', '<PROJECT_LUA_VM>', '<PROJECT_UI_SYSTEM>', '<PROJECT_RELOAD_MODULE>'):
        assert name in text
    assert '公开脱敏适配模板' in text

def test_public_lua_adapter_has_example_namespace():
    source=(root/'unity-bridge/com.local.unitymcp-bridge/Editor/UnityMCPCommands.cs').read_text(encoding='utf8')
    assert 'PROJECT_NAMESPACE.PROJECT_FACADE' in source
    assert 'PROJECT_LUA_MANAGER' in source
    assert 'PROJECT_LUA_VM' in source
