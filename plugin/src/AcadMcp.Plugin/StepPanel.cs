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

    /// <summary>Whether the palette is on screen.</summary>
    public static bool IsVisible => _palette?.Visible ?? false;

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
            // AutoCAD remembers the size and position for this id, so the minimum
            // is what stops a restored-too-small palette from squeezing the detail
            // pane down to nothing.
            MinimumSize = new Size(440, 560),
            Size = new Size(470, 760),
            // Without this, clicking the palette takes focus from the drawing, which
            // makes it awkward to review a plan you are looking at.
            KeepFocus = true,
        };
        palette.Add("Steps", new StepsControl());
        return palette;
    }
}
