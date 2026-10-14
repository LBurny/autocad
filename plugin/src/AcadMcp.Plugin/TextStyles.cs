using Autodesk.AutoCAD.DatabaseServices;

namespace AcadMcp.Plugin;

/// <summary>
/// The text style this plugin draws with, and why it is not the drawing default.
/// </summary>
/// <remarks>
/// <para>
/// A stock drawing's <c>Standard</c> style is <c>txt.shx</c> with no big font, and
/// an SHX font without a big font has no CJK glyphs at all: every Chinese
/// character comes out as <c>?</c>. The <c>gbenor.shx</c> + <c>gbcbig.shx</c> pair
/// carries them. That was measured, not assumed — on this machine a CJK TrueType
/// face drew <c>?</c> in AutoCAD while this pair drew correctly.
/// </para>
/// <para>
/// The Python side came to the same conclusion in <c>backends/com.py</c>, so text
/// drawn through either driver looks alike. Keep the two in step.
/// </para>
/// </remarks>
internal static class TextStyles
{
    /// <summary>Name of the style, chosen to match the COM backend's.</summary>
    public const string Name = "HZ";

    private const string Font = "gbenor.shx";
    private const string BigFont = "gbcbig.shx";

    /// <summary>
    /// Create the style, or repair it if it is present with the wrong fonts, and
    /// return its id.
    /// </summary>
    /// <remarks>
    /// Repairing matters because the style is named, not owned: a drawing that
    /// came from elsewhere may already have an <c>HZ</c> pointing at a font with
    /// no big font, and inheriting that would silently reintroduce the question
    /// marks. The check runs first so the common case stays a pure read — the
    /// write is skipped when the fonts are already right, which keeps this out of
    /// every step's transaction.
    /// </remarks>
    public static ObjectId Ensure(Database db, Transaction tr)
    {
        var table = (TextStyleTable)tr.GetObject(db.TextStyleTableId, OpenMode.ForRead);

        TextStyleTableRecord record;
        if (table.Has(Name))
        {
            record = (TextStyleTableRecord)tr.GetObject(table[Name], OpenMode.ForRead);
            if (IsCorrect(record))
            {
                return record.ObjectId;
            }
            record.UpgradeOpen();
        }
        else
        {
            table.UpgradeOpen();
            record = new TextStyleTableRecord { Name = Name };
            table.Add(record);
            tr.AddNewlyCreatedDBObject(record, true);
        }

        // SHX fonts go on FileName, not Font: Font takes a TrueType family name,
        // and handing it "gbenor.shx" is rejected with eInvalidInput. This mirrors
        // the COM backend, which sets fontFile and bigFontFile.
        record.FileName = Font;
        record.BigFontFileName = BigFont;
        record.XScale = 1.0;
        return record.ObjectId;
    }

    private static bool IsCorrect(TextStyleTableRecord record) =>
        string.Equals(record.FileName, Font, StringComparison.OrdinalIgnoreCase)
        && string.Equals(record.BigFontFileName, BigFont, StringComparison.OrdinalIgnoreCase);
}
