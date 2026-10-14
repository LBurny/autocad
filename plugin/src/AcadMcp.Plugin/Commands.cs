using Autodesk.AutoCAD.Runtime;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace AcadMcp.Plugin;

/// <summary>Commands the user can type. The MCP side calls the step engine directly.</summary>
public sealed class Commands
{
    [CommandMethod("ACADMCPHELLO")]
    public static void Hello()
    {
        try
        {
            var version = typeof(Commands).Assembly.GetName().Version;
            PluginLog.Write($"ACADMCPHELLO: version {version}");
            var doc = AcadApp.DocumentManager.MdiActiveDocument;
            doc?.Editor.WriteMessage($"\nAcadMcp {version} loaded; opening the steps palette.");
            StepPanel.Show();
            PluginLog.Write("ACADMCPHELLO: palette shown");
        }
        catch (System.Exception error)
        {
            PluginLog.Write("ACADMCPHELLO failed", error);
            throw;
        }
    }
}
