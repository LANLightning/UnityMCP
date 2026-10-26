from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


POLICY_SOURCE = Path(
    "unity-bridge/com.local.unitymcp-bridge/Editor/UnityMCPListenerPortPolicy.cs"
)


def test_listener_port_policy_compiles_and_enforces_fallback_contract() -> None:
    assert POLICY_SOURCE.is_file(), f"missing listener port policy: {POLICY_SOURCE}"
    if not shutil.which("dotnet"):
        pytest.skip("dotnet SDK is not installed; skipping the optional C# policy harness")

    program = r'''
using System;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using UnityMCP.Bridge;

static class Program
{
    private static void Expect(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }

    private static TcpListener Bind(int port)
    {
        var listener = new TcpListener(IPAddress.Loopback, port);
        listener.Server.ExclusiveAddressUse = true;
        listener.Start();
        return listener;
    }

    private static int FindConsecutivePorts(out TcpListener first)
    {
        for (int port = 30000; port < 60000; port++)
        {
            TcpListener candidate = null;
            TcpListener neighbor = null;
            try
            {
                candidate = Bind(port);
                neighbor = Bind(port + 1);
                neighbor.Stop();
                first = candidate;
                return port;
            }
            catch (SocketException)
            {
                try { neighbor?.Stop(); } catch { }
                try { candidate?.Stop(); } catch { }
            }
        }
        throw new InvalidOperationException("could not find consecutive test ports");
    }

    public static void Main()
    {
        ListenerPortOptions defaults = UnityMCPListenerPortPolicy.Resolve(null, null, null);
        Expect(defaults.PreferredPort == 17873, "default port");
        Expect(defaults.AllowFallback, "default port should tolerate collisions");
        Expect(defaults.FallbackPortCount == 10, "default fallback count");
        Expect(UnityMCPListenerPortPolicy.EnumerateCandidates(defaults).SequenceEqual(
            Enumerable.Range(17873, 11)), "default candidate sequence");

        ListenerPortOptions pinned = UnityMCPListenerPortPolicy.Resolve("19001", null, null);
        Expect(pinned.PreferredPort == 19001, "explicit port");
        Expect(!pinned.AllowFallback, "explicit port must be strict by default");
        Expect(UnityMCPListenerPortPolicy.EnumerateCandidates(pinned).SequenceEqual(
            new[] { 19001 }), "strict explicit candidate sequence");

        ListenerPortOptions optedIn = UnityMCPListenerPortPolicy.Resolve("19001", "true", "2");
        Expect(optedIn.AllowFallback, "explicit fallback opt-in");
        Expect(optedIn.FallbackPortCount == 2, "explicit fallback count");
        Expect(UnityMCPListenerPortPolicy.EnumerateCandidates(optedIn).SequenceEqual(
            new[] { 19001, 19002, 19003 }), "opt-in candidate sequence");

        ListenerPortOptions nearLimit = UnityMCPListenerPortPolicy.Resolve("65534", "1", "10");
        Expect(UnityMCPListenerPortPolicy.EnumerateCandidates(nearLimit).SequenceEqual(
            new[] { 65534, 65535 }), "candidate sequence must stop at 65535");

        foreach (string invalidPort in new[] { "not-a-port", "0", "65536" })
        {
            bool rejected = false;
            try
            {
                UnityMCPListenerPortPolicy.Resolve(invalidPort, null, null);
            }
            catch (ArgumentException)
            {
                rejected = true;
            }
            Expect(rejected, "invalid explicit port must fail closed: " + invalidPort);
        }

        ListenerPortOptions capped = UnityMCPListenerPortPolicy.Resolve(null, null, "999");
        Expect(capped.FallbackPortCount == 100, "fallback count is capped");

        Expect(UnityMCPListenerPortPolicy.ComputeRetryDelaySeconds(1) == 0.25,
            "first retry delay");
        Expect(UnityMCPListenerPortPolicy.ComputeRetryDelaySeconds(2) == 0.5,
            "second retry delay");
        Expect(UnityMCPListenerPortPolicy.ComputeRetryDelaySeconds(20) == 30.0,
            "retry delay must be capped");

        TcpListener blocker;
        int preferred = FindConsecutivePorts(out blocker);
        TcpListener started = null;
        TcpListener secondBlocker = null;
        try
        {
            var fallbackOptions = new ListenerPortOptions(preferred, true, 1);
            bool recovered = UnityMCPListenerFactory.TryStart(
                fallbackOptions, out started, out int selected,
                out var attempted, out Exception startError);
            Expect(recovered, "occupied preferred port should recover");
            Expect(selected == preferred + 1, "fallback selected port");
            Expect(attempted.SequenceEqual(new[] { preferred, preferred + 1 }),
                "fallback attempted ports");
            Expect(startError == null, "successful fallback clears error");
            started.Stop();
            started = null;

            var strictOptions = new ListenerPortOptions(preferred, false, 10);
            bool strictStarted = UnityMCPListenerFactory.TryStart(
                strictOptions, out started, out selected, out attempted, out startError);
            Expect(!strictStarted, "strict occupied port must fail");
            Expect(started == null, "strict failure must not leak a listener");
            Expect(attempted.SequenceEqual(new[] { preferred }), "strict attempted ports");

            secondBlocker = Bind(preferred + 1);
            bool exhausted = UnityMCPListenerFactory.TryStart(
                fallbackOptions, out started, out selected, out attempted, out startError);
            Expect(!exhausted, "fully occupied range must fail");
            Expect(started == null, "exhausted range must not leak a listener");
        }
        finally
        {
            try { started?.Stop(); } catch { }
            try { secondBlocker?.Stop(); } catch { }
            try { blocker?.Stop(); } catch { }
        }

        var recovery = new ListenerRecoveryState();
        Expect(recovery.Schedule(100.0) == 0.25, "first schedule delay");
        Expect(recovery.Attempts == 1 && recovery.NextRetryAt == 100.25,
            "first schedule state");
        recovery.Schedule(200.0);
        Expect(recovery.Attempts == 2 && recovery.NextRetryAt == 200.5,
            "second schedule state");
        recovery.Reset();
        Expect(recovery.Attempts == 0 && recovery.NextRetryAt == 0.0,
            "successful recovery resets retry state");
    }
}
'''

    project = r'''
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>disable</Nullable>
  </PropertyGroup>
</Project>
'''

    with tempfile.TemporaryDirectory(prefix="unitymcp-port-policy-") as temp_dir:
        temp = Path(temp_dir)
        (temp / "Policy.cs").write_text(POLICY_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
        (temp / "Program.cs").write_text(program, encoding="utf-8")
        (temp / "PolicyHarness.csproj").write_text(project, encoding="utf-8")
        result = subprocess.run(
            ["dotnet", "run", "--project", str(temp / "PolicyHarness.csproj"), "--nologo"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )

    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
