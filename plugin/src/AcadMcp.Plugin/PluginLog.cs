using System.Globalization;
using System.IO;
using System.Runtime.CompilerServices;

namespace AcadMcp.Plugin;

/// <summary>
/// Append-only log in the temp directory.
/// </summary>
/// <remarks>
/// When a plugin fails to load, AutoCAD prints to the command line — awkward to
/// read programmatically and easy to miss behind a dialog. Everything this
/// assembly does at load and command time lands here instead, so a failure has
/// somewhere visible to be. Logging must never be the thing that breaks a load,
/// hence the swallowed exceptions.
/// </remarks>
public static class PluginLog
{
    private static readonly object Gate = new();

    public static string LogPath { get; } =
        Path.Combine(Path.GetTempPath(), "acadmcp-plugin.log");

    [ModuleInitializer]
    internal static void OnModuleLoad() => Write("module loaded");

    public static void Write(string message)
    {
        try
        {
            var line = string.Format(
                CultureInfo.InvariantCulture,
                "{0:yyyy-MM-dd HH:mm:ss.fff}  {1}{2}",
                DateTime.Now,
                message,
                Environment.NewLine);
            lock (Gate)
            {
                File.AppendAllText(LogPath, line);
            }
        }
        catch
        {
            // Never let logging abort the caller.
        }
    }

    public static void Write(string message, Exception error) =>
        Write($"{message}: {error.GetType().Name}: {error.Message}\n{error.StackTrace}");
}
