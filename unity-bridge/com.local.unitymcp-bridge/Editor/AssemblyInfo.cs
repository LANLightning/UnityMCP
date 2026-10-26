// AssemblyInfo.cs
// Exposes internal types of the bridge Editor assembly to the standalone test
// assembly so pure decision helpers (e.g. VerificationSession.ClassifyVerdict)
// can be unit-tested without widening the public API surface.

using System.Runtime.CompilerServices;

[assembly: InternalsVisibleTo("com.local.unitymcp-bridge.Tests.Editor")]
