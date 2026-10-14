using System.Drawing;
using System.Windows.Forms;
using Autodesk.AutoCAD.Windows;

namespace AcadMcp.Plugin;

/// <summary>
/// The dockable steps palette.
/// </summary>
/// <remarks>
/// Created lazily on first use and reused afterwards, so loading the assembly
/// has no side effect on the user's screen. The palette is a component, not a
/// control, so it is disposed rather than closed.
/// </remarks>
public static class StepPanel
{
    /// <summary>Stable id so AutoCAD remembers the palette's dock position.</summary>
    private static readonly Guid ToolId = new("8f0a6f0e-2a2f-4a4f-9f4e-2c9b6b1a7d31");

    private static PaletteSet? _palette;

    public static void Show()
    {
        _palette ??= Build();
        _palette.Visible = true;
    }

    public static void Hide()
    {
        if (_palette is not null)
        {
            _palette.Visible = false;
        }
    }

    public static void Destroy()
    {
        _palette?.Dispose();
        _palette = null;
    }

    private static PaletteSet Build()
    {
        var palette = new PaletteSet("AcadMcp Steps", ToolId)
        {
            Style = PaletteSetStyles.ShowPropertiesMenu
                | PaletteSetStyles.ShowAutoHideButton
                | PaletteSetStyles.ShowCloseButton,
            MinimumSize = new Size(360, 460),
        };
        palette.Add("Steps", new StepsControl());
        return palette;
    }
}
