using System.Drawing;
using System.Windows.Forms;

namespace AcadMcp.Plugin;

/// <summary>Placeholder body of the steps palette, replaced by the real panel later.</summary>
/// <remarks>
/// Phase 0 only needs to prove that WinForms hosts inside a PaletteSet in this
/// AutoCAD build, so this deliberately shows little beyond proof of life.
/// </remarks>
public sealed class StepsControl : UserControl
{
    public StepsControl()
    {
        Dock = DockStyle.Fill;
        BackColor = Color.FromArgb(30, 30, 30);
        ForeColor = Color.Gainsboro;
        Padding = new Padding(10);

        var header = new Label
        {
            Dock = DockStyle.Top,
            Height = 28,
            Text = "AcadMcp — step journal",
            Font = new Font("Segoe UI", 10f, FontStyle.Bold),
            ForeColor = Color.Gainsboro,
        };

        var body = new Label
        {
            Dock = DockStyle.Fill,
            Text = "No steps recorded yet.\n\nThe palette loads, so the plugin\nroute works in this AutoCAD build.",
            ForeColor = Color.Silver,
        };

        Controls.Add(body);
        Controls.Add(header);
    }
}
