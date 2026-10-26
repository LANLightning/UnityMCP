# Multi-Bridge Management Feature

**New in v0.11.0**: Support for managing multiple Unity Editor instances simultaneously.

## Quick Start

When running multiple Unity Editor instances, each must use a different port. Use the `select_bridge` tool to scan and switch between them.

### 1. Configure Unity Instances

```powershell
# Instance 1 - Default port 17873
Start-Process Unity.exe -ArgumentList "-projectPath", "<PROJECT_ROOT>"

# Instance 2 - Port 17874
$env:UNITY_MCP_BRIDGE_PORT = "17874"
Start-Process Unity.exe -ArgumentList "-projectPath", "<PROJECT_ROOT>"
```

### 2. Scan Available Instances

```python
select_bridge(scan=True)
# Returns list of all running instances with project paths, scenes, and ports
```

### 3. Switch Connection

```python
select_bridge(port=17874)  # Connect to instance on port 17874
```

### 4. Check Current Status

```python
select_bridge()  # Shows current connection port and status
```

## Features

✅ **Auto-discovery**: Scan ports 17873-17882 for running instances  
✅ **Smart switching**: Automatic reconnection when changing ports  
✅ **Status tracking**: Always know which instance you're connected to  
✅ **Batch operations**: Loop through multiple instances easily  
✅ **Error handling**: Graceful handling of connection failures  

## Use Cases

- **Parallel development**: Work on multiple projects simultaneously
- **Version comparison**: Test features across different builds
- **Batch operations**: Screenshot or test multiple instances
- **Demo preparation**: Manage demo and dev environments

## Documentation

- 📖 [Full Guide](docs/MULTI_BRIDGE.md) - Complete usage documentation
- ⚙️ [Unity Configuration](docs/UNITY_PORT_CONFIG.md) - How to configure Unity ports
- 🚀 [Quick Reference](docs/SELECT_BRIDGE_QUICK_REF.md) - Cheat sheet
- 📝 [Examples](examples/multi_bridge_example.py) - Working code samples

## Example Workflow

```python
# 1. Find all instances
result = select_bridge(scan=True)
print(f"Found {result['found_instances']} Unity instances")

# 2. Work with first project
select_bridge(port=17873)
lua_eval(script="return PROJECT_RUNTIME.me.level", goal="Get player level")

# 3. Switch to second project
select_bridge(port=17874)
capture_screenshot()

# 4. Verify current connection
status = select_bridge()
print(f"Connected to port {status['port']}")
```

## Troubleshooting

**Issue**: Handshake timeout  
**Solution**: Run `select_bridge(scan=True)` to see which ports are responding

**Issue**: Connected to wrong instance  
**Solution**: Use `select_bridge()` to check current port, then switch with `select_bridge(port=...)`

**Issue**: No instances found  
**Solution**: Ensure Unity instances are running and configured with different ports

---

For more details, see [MULTI_BRIDGE.md](docs/MULTI_BRIDGE.md).
