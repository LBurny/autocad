using Autodesk.AutoCAD.Runtime;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;

[assembly: ExtensionApplication(typeof(AcadMcp.Plugin.PluginEntry))]

namespace AcadMcp.Plugin;

/// <summary>
/// Load and unload hook AutoCAD calls when the assembly is loaded.
/// </summary>
/// <remarks>
/// <see cref="Initialize"/> stays side-effect free: with the autoloader this
/// runs at every AutoCAD start, and popping a palette on the user's screen
/// without being asked would be hostile. Work that must always run (the RPC
/// listener) attaches here later, deliberately without touching the UI.
/// </remarks>
public sealed class PluginEntry : IExtensionApplication
{
    /// <summary>
    /// Set <c>ACADMCP_SELFTEST=1</c> in the environment to have the plugin run
    /// its self-test once at startup.
    /// </summary>
    /// <remarks>
    /// A loaded plugin assembly cannot be unloaded, so the only way to test a
    /// build is to start AutoCAD with it already installed. This lets a fresh
    /// AutoCAD verify itself unattended, which is what makes the edit-build-test
    /// loop bearable — and it keeps the test out of the user's own session.
    /// </remarks>
    private const string SelfTestVariable = "ACADMCP_SELFTEST";

    public void Initialize()
    {
        try
        {
            var name = typeof(PluginEntry).Assembly.GetName();
            PluginLog.Write($"initialize: {name.Name} {name.Version}");

            // Initialize runs on AutoCAD's main thread, which is why the queue can
            // learn here which thread it must never be waited on from.
            IdleQueue.Attach();
            StartRpc();

            if (Environment.GetEnvironmentVariable(SelfTestVariable) == "1")
            {
                ScheduleSelfTest();
            }
        }
        catch (System.Exception error)
        {
            PluginLog.Write("initialize failed", error);
        }
    }

    /// <summary>
    /// Open the RPC port, or carry on without it.
    /// </summary>
    /// <remarks>
    /// A second AutoCAD on the same machine, or any other process sitting on the
    /// port, must not stop the plugin from loading: the palette works in-process
    /// and needs no socket. So a failure here is reported and forgotten.
    /// </remarks>
    private static void StartRpc()
    {
        var port = RpcServer.ConfiguredPort();
        try
        {
            RpcServer.Start(port);
            PluginLog.Write($"rpc listening on 127.0.0.1:{port}");
        }
        catch (System.Exception error)
        {
            PluginLog.Write($"rpc could not bind 127.0.0.1:{port}: {error.Message}");
        }
    }

    private static void ScheduleSelfTest()
    {
        PluginLog.Write("auto self-test armed; waiting for the first idle");

        EventHandler? handler = null;
        handler = (_, _) =>
        {
            // Not yet ready during Initialize: the first idle is the earliest
            // point at which a document and the command context exist.
            AcadApp.Idle -= handler;
            var deferred = false;
            try
            {
                deferred = SelfTest.RunUndoProbe();
            }
            catch (System.Exception error)
            {
                PluginLog.Write("auto self-test threw", error);
            }
            finally
            {
                if (!deferred)
                {
                    // A marker the harness can wait for, so it never has to guess
                    // whether the run finished or the log was simply not flushed.
                    // The undo probe writes it itself, once its second phase has
                    // reported.
                    PluginLog.Write("AUTO-SELFTEST-DONE");
                }
            }
        };
        AcadApp.Idle += handler;
    }

    public void Terminate()
    {
        PluginLog.Write("terminate");
        RpcServer.Current?.Dispose();
        IdleQueue.Detach();
        StepPanel.Destroy();
    }
}
