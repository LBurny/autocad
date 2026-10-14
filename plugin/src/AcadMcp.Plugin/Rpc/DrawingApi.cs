using System.Text.Json.Nodes;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;

namespace AcadMcp.Plugin;

/// <summary>
/// The document-level calls the RPC surface needs that are not steps: layers,
/// units and reading the model space back out.
/// </summary>
/// <remarks>
/// These are deliberately separate from <see cref="StepEngine"/>. Layers and
/// units are document state rather than drawing content: creating a layer is
/// idempotent and is not something a rollback should undo, so it happens outside
/// the journal instead of being recorded as a step.
/// </remarks>
internal static class DrawingApi
{
    /// <summary>Open a new drawing and return its name.</summary>
    /// <remarks>
    /// Needed because AutoCAD can be running with nothing but the Start tab up, in
    /// which case there is no active document for anything else to act on. It is
    /// also what makes a freshly launched AutoCAD usable over RPC.
    /// </remarks>
    public static JsonObject NewDocument()
    {
        var document = AcadApp.DocumentManager.Add("");
        return new JsonObject { ["document"] = document.Name };
    }

    public static JsonObject EnsureLayer(Document document, string name, int? color, string? linetype)
    {
        if (!string.IsNullOrWhiteSpace(linetype) && linetype != "CONTINUOUS")
        {
            LoadLinetype(document.Database, linetype);
        }

        using var docLock = document.LockDocument();
        using var tr = document.Database.TransactionManager.StartTransaction();
        var table = (LayerTable)tr.GetObject(document.Database.LayerTableId, OpenMode.ForRead);

        var existed = table.Has(name);
        if (!existed)
        {
            table.UpgradeOpen();
            var record = new LayerTableRecord { Name = name };
            table.Add(record);
            tr.AddNewlyCreatedDBObject(record, true);
        }

        if (color is { } aci)
        {
            var record = (LayerTableRecord)tr.GetObject(table[name], OpenMode.ForWrite);
            // Qualified: the plugin project also pulls in System.Drawing, so a bare
            // Color is ambiguous.
            record.Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(
                ColorMethod.ByAci, (short)aci);
        }
        if (!string.IsNullOrWhiteSpace(linetype) && linetype != "CONTINUOUS")
        {
            var linetypes = (LinetypeTable)tr.GetObject(document.Database.LinetypeTableId, OpenMode.ForRead);
            if (linetypes.Has(linetype))
            {
                var record = (LayerTableRecord)tr.GetObject(table[name], OpenMode.ForWrite);
                record.LinetypeObjectId = linetypes[linetype];
            }
        }

        tr.Commit();
        return new JsonObject { ["layer"] = name, ["created"] = !existed };
    }

    public static JsonObject SetUnitsMm(Document document)
    {
        using var docLock = document.LockDocument();
        document.Database.Insunits = UnitsValue.Millimeters;
        return new JsonObject { ["insunits"] = 4, ["units"] = "millimetres" };
    }

    public static JsonObject Info(Document document)
    {
        var db = document.Database;
        using var docLock = document.LockDocument();
        using var tr = db.TransactionManager.StartTransaction();

        var layers = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
        var model = (BlockTableRecord)tr.GetObject(
            SymbolUtilityServices.GetBlockModelSpaceId(db), OpenMode.ForRead);

        return new JsonObject
        {
            ["document"] = document.Name,
            ["path"] = document.Database.Filename,
            ["insunits"] = (int)db.Insunits,
            ["units"] = Units(db.Insunits),
            // Counted through LINQ: the symbol table types are enumerable but expose
            // no usable Count member.
            ["layers"] = layers.Cast<ObjectId>().Count(),
            ["entities"] = model.Cast<ObjectId>().Count(),
        };
    }

    /// <summary>Entities in the model space, optionally filtered to one layer.</summary>
    public static JsonArray Entities(Document document, string? layer, int limit)
    {
        var db = document.Database;
        var found = new JsonArray();
        using var docLock = document.LockDocument();
        using var tr = db.TransactionManager.StartTransaction();

        var model = (BlockTableRecord)tr.GetObject(
            SymbolUtilityServices.GetBlockModelSpaceId(db), OpenMode.ForRead);

        foreach (var id in model)
        {
            if (found.Count >= limit)
            {
                break;
            }
            if (tr.GetObject(id, OpenMode.ForRead) is not Entity entity || entity.IsErased)
            {
                continue;
            }
            if (!string.IsNullOrWhiteSpace(layer)
                && !string.Equals(entity.Layer, layer, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var item = new JsonObject
            {
                ["handle"] = entity.Handle.ToString(),
                ["type"] = entity.GetRXClass().Name,
                ["layer"] = entity.Layer,
            };
            if (entity is DBText text)
            {
                item["text"] = text.TextString;
            }
            else if (entity is MText mtext)
            {
                item["text"] = mtext.Contents;
            }
            found.Add(item);
        }
        tr.Commit();
        return found;
    }

    private static void LoadLinetype(Database db, string name)
    {
        var linetypes = (LinetypeTable)db.LinetypeTableId.GetObject(OpenMode.ForRead);
        if (linetypes.Has(name))
        {
            return;
        }
        try
        {
            db.LoadLineTypeFile(name, "acad.lin");
        }
        catch (System.Exception)
        {
            // Cosmetic only: a missing linetype is not worth refusing a layer for.
        }
    }

    private static string Units(UnitsValue value) => value switch
    {
        UnitsValue.Millimeters => "millimetres",
        UnitsValue.Centimeters => "centimetres",
        UnitsValue.Meters => "metres",
        UnitsValue.Inches => "inches",
        UnitsValue.Feet => "feet",
        _ => "unitless",
    };
}
