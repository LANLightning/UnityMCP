using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;

namespace UnityMCP.Bridge
{
    internal sealed class ListenerPortOptions
    {
        public int PreferredPort { get; }
        public bool AllowFallback { get; }
        public int FallbackPortCount { get; }

        public ListenerPortOptions(int preferredPort, bool allowFallback, int fallbackPortCount)
        {
            PreferredPort = preferredPort;
            AllowFallback = allowFallback;
            FallbackPortCount = fallbackPortCount;
        }
    }

    /// <summary>
    /// Pure policy for listener port selection and retry timing. Keeping this separate
    /// from Unity and socket APIs makes collision behavior deterministic and testable.
    /// </summary>
    internal static class UnityMCPListenerPortPolicy
    {
        internal const int DefaultPort = 17873;
        internal const int DefaultFallbackPortCount = 10;
        internal const int MaxFallbackPortCount = 100;
        internal const double MaxRetryDelaySeconds = 30.0;

        internal static ListenerPortOptions Resolve(
            string configuredPort,
            string configuredAllowFallback,
            string configuredFallbackPortCount)
        {
            bool hasExplicitPort = !string.IsNullOrWhiteSpace(configuredPort);
            int preferredPort;
            if (hasExplicitPort)
            {
                if (!int.TryParse(configuredPort, out preferredPort) ||
                    preferredPort < 1 || preferredPort > 65535)
                {
                    throw new ArgumentException(
                        "UNITY_MCP_BRIDGE_PORT must be an integer from 1 through 65535.",
                        nameof(configuredPort));
                }
            }
            else
            {
                preferredPort = DefaultPort;
            }

            bool allowFallback = !hasExplicitPort;
            if (TryParseBoolean(configuredAllowFallback, out bool configuredFallback))
                allowFallback = configuredFallback;

            int fallbackPortCount = DefaultFallbackPortCount;
            if (int.TryParse(configuredFallbackPortCount, out int parsedCount))
                fallbackPortCount = Math.Max(0, Math.Min(MaxFallbackPortCount, parsedCount));

            return new ListenerPortOptions(preferredPort, allowFallback, fallbackPortCount);
        }

        internal static IEnumerable<int> EnumerateCandidates(ListenerPortOptions options)
        {
            if (options == null)
                throw new ArgumentNullException(nameof(options));

            int additionalPorts = options.AllowFallback ? options.FallbackPortCount : 0;
            int lastPort = Math.Min(65535, options.PreferredPort + additionalPorts);
            for (int port = options.PreferredPort; port <= lastPort; port++)
                yield return port;
        }

        internal static double ComputeRetryDelaySeconds(int attempt)
        {
            int safeAttempt = Math.Max(1, Math.Min(attempt, 30));
            double delay = 0.25 * Math.Pow(2.0, safeAttempt - 1);
            return Math.Min(MaxRetryDelaySeconds, delay);
        }

        private static bool TryParseBoolean(string value, out bool parsed)
        {
            if (string.Equals(value, "1", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(value, "true", StringComparison.OrdinalIgnoreCase))
            {
                parsed = true;
                return true;
            }

            if (string.Equals(value, "0", StringComparison.OrdinalIgnoreCase) ||
                string.Equals(value, "false", StringComparison.OrdinalIgnoreCase))
            {
                parsed = false;
                return true;
            }

            parsed = false;
            return false;
        }
    }

    /// <summary>
    /// Owns the real socket bind loop. It has no Unity dependency, so collision and
    /// cleanup behavior can be exercised by the standalone test harness.
    /// </summary>
    internal static class UnityMCPListenerFactory
    {
        internal static bool TryStart(
            ListenerPortOptions options,
            out TcpListener listener,
            out int selectedPort,
            out List<int> attemptedPorts,
            out Exception lastError)
        {
            listener = null;
            selectedPort = options.PreferredPort;
            attemptedPorts = new List<int>();
            lastError = null;

            foreach (int candidatePort in UnityMCPListenerPortPolicy.EnumerateCandidates(options))
            {
                attemptedPorts.Add(candidatePort);
                TcpListener candidate = null;
                try
                {
                    candidate = new TcpListener(IPAddress.Loopback, candidatePort);
                    candidate.Server.ExclusiveAddressUse = true;
                    candidate.Start();
                    listener = candidate;
                    selectedPort = candidatePort;
                    lastError = null;
                    return true;
                }
                catch (SocketException ex)
                {
                    lastError = ex;
                    try { candidate?.Stop(); } catch { /* best-effort */ }
                    if (ex.SocketErrorCode != SocketError.AddressAlreadyInUse)
                        break;
                }
                catch (Exception ex)
                {
                    lastError = ex;
                    try { candidate?.Stop(); } catch { /* best-effort */ }
                    break;
                }
            }

            return false;
        }
    }

    internal sealed class ListenerRecoveryState
    {
        internal int Attempts { get; private set; }
        internal double NextRetryAt { get; private set; }

        internal double Schedule(double now)
        {
            Attempts++;
            double delay = UnityMCPListenerPortPolicy.ComputeRetryDelaySeconds(Attempts);
            NextRetryAt = now + delay;
            return delay;
        }

        internal void Reset()
        {
            Attempts = 0;
            NextRetryAt = 0.0;
        }
    }
}
