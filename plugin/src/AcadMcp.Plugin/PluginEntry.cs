using Autodesk.AutoCAD.Runtime;

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
    public void Initialize()
    {
        try
        {
            var name = typeof(PluginEntry).Assembly.GetName();
            PluginLog.Write($"initialize: {name.Name} {name.Version}");
        }
        catch (System.Exception error)
        {
            PluginLog.Write("initialize failed", error);
        }
    }

    public void Terminate()
    {
        PluginLog.Write("terminate");
        StepPanel.Destroy();
    }
}
