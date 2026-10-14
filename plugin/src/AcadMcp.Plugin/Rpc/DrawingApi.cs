using System.Text.Json.Nodes;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.Colors;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
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
        var wantsLinetype = !string.IsNullOrWhiteSpace(linetype) && linetype != "CONTINUOUS";

        using var docLock = document.LockDocument();
        using var tr = document.Database.TransactionManager.StartTransaction();

        // Inside the transaction, not before it: reading the linetype table needs one
        // open, and outside one the lookup hands back null — which used to surface as
        // a NullReferenceException for any layer that carried a linetype.
        if (wantsLinetype)
        {
            LoadLinetype(document.Database, linetype!, tr);
        }

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
        if (wantsLinetype)
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

    /// <summary>Block definitions in this drawing, or in a library file when asked for one.</summary>
    /// <remarks>
    /// Read-only both ways: the open drawing is only read, and a library file is opened on
    /// the side and never written. Anonymous, layout and externally referenced definitions
    /// are left out — nobody inserts those, so a catalogue built from this list must not
    /// offer them either.
    /// </remarks>
    public static JsonArray Blocks(Document document, string? source, int limit)
    {
        if (!string.IsNullOrWhiteSpace(source))
        {
            if (!File.Exists(source))
            {
                throw new FileNotFoundException($"no such library file: {source}", source);
            }

            // On the side, because the user may well have the library open in AutoCAD.
            using var library = new Database(false, true);
            library.ReadDwgFile(source, FileOpenMode.OpenForReadAndAllShare, false, null);
            using var libraryTr = library.TransactionManager.StartTransaction();
            var listed = ListBlocks(library, libraryTr, limit);
            libraryTr.Abort();
            return listed;
        }

        using var docLock = document.LockDocument();
        using var tr = document.Database.TransactionManager.StartTransaction();
        var blocks = ListBlocks(document.Database, tr, limit);
        tr.Commit();
        return blocks;
    }

    /// <summary>Make a block definition available in this drawing, importing it if needed.</summary>
    /// <remarks>
    /// Document state, not a step: idempotent, and outside the journal, for the reason
    /// given on <see cref="StepEngine.EnsureBlock"/>.
    /// </remarks>
    public static JsonObject EnsureBlock(Document document, string name, string? source)
    {
        using var docLock = document.LockDocument();
        using var tr = document.Database.TransactionManager.StartTransaction();
        StepEngine.EnsureBlock(document.Database, tr, name, source, out var imported);
        tr.Commit();
        return new JsonObject { ["block"] = name, ["created"] = imported };
    }

    private static JsonArray ListBlocks(Database db, Transaction tr, int limit)
    {
        var found = new JsonArray();
        var table = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);

        foreach (var id in table)
        {
            if (found.Count >= limit)
            {
                break;
            }
            if (tr.GetObject(id, OpenMode.ForRead) is not BlockTableRecord definition)
            {
                continue;
            }
            if (definition.IsAnonymous || definition.IsLayout
                || definition.IsFromExternalReference || definition.IsFromOverlayReference)
            {
                continue;
            }

            found.Add(DescribeDefinition(definition));
        }
        return found;
    }

    private static JsonObject DescribeDefinition(BlockTableRecord definition)
    {
        var item = new JsonObject
        {
            ["name"] = definition.Name,
            ["is_dynamic"] = definition.IsDynamicBlock,
            ["explodable"] = definition.Explodable,
            // Counted through LINQ: a block record is enumerable but exposes no usable
            // Count member.
            ["entities"] = definition.Cast<ObjectId>().Count(),
        };

        if (definition.IsDynamicBlock)
        {
            item["properties"] = DynamicParameters(definition);
        }
        return item;
    }

    /// <summary>What a dynamic block can be told to be: its parameters and their values.</summary>
    /// <remarks>
    /// Read off a throwaway reference, because the collection belongs to a reference and
    /// not to the definition. It is never appended to anything: asking a definition about
    /// itself must not leave an entity behind in the drawing.
    /// </remarks>
    private static JsonArray DynamicParameters(BlockTableRecord definition)
    {
        var parameters = new JsonArray();
        using var reference = new BlockReference(Point3d.Origin, definition.ObjectId);
        if (!reference.IsDynamicBlock)
        {
            return parameters;
        }

        var collection = reference.DynamicBlockReferencePropertyCollection;
        for (var i = 0; i < collection.Count; i++)
        {
            var property = collection[i];
            var values = new JsonArray();
            foreach (var allowed in property.GetAllowedValues() ?? [])
            {
                values.Add(Distinguish(allowed));
            }

            parameters.Add(new JsonObject
            {
                ["name"] = property.PropertyName,
                ["read_only"] = property.ReadOnly,
                // A unit type of 2 is a distance, so 400 there means 400 mm rather than a
                // ratio: worth carrying, because a size parameter and a count parameter
                // look identical once they are just numbers in JSON.
                ["units"] = property.UnitsType.ToString(),
                ["visible"] = property.VisibleInCurrentVisibilityState,
                ["values"] = values,
            });
        }
        return parameters;
    }

    /// <summary>Render an allowed value, keeping numbers distinguishable from text.</summary>
    private static JsonNode? Distinguish(object? allowed) => allowed switch
    {
        null => null,
        double number => JsonValue.Create(number),
        short number => JsonValue.Create(number),
        int number => JsonValue.Create(number),
        long number => JsonValue.Create(number),
        bool flag => JsonValue.Create(flag),
        _ => JsonValue.Create(allowed.ToString()),
    };

    /// <summary>
    /// What a placed block reference was set to: its live dynamic parameters.
    /// </summary>
    /// <remarks>
    /// Only the ones that are visible and settable, because those are the ones anything can
    /// do anything about. Read-only ones (AutoCAD puts an "Origin" on every dynamic block) are
    /// left out rather than reported as if they were settings.
    /// </remarks>
    private static JsonObject ReferenceProperties(BlockReference reference)
    {
        var properties = new JsonObject();
        if (!reference.IsDynamicBlock)
        {
            return properties;
        }

        var collection = reference.DynamicBlockReferencePropertyCollection;
        for (var i = 0; i < collection.Count; i++)
        {
            var property = collection[i];
            if (property.ReadOnly || !property.VisibleInCurrentVisibilityState)
            {
                continue;
            }
            properties[property.PropertyName] = Distinguish(property.Value);
        }
        return properties;
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
            else if (entity is BlockReference reference)
            {
                // Both names, because they are not always the same and which one answers
                // "what is this?" depends on the question. Name is what the reference calls
                // itself; the definition is the record it resolves to, and AutoCAD keeps that
                // as an anonymous *U record for a dynamic block set to a custom state.
                item["block"] = reference.Name;
                item["definition"] = reference.BlockTableRecord.IsNull
                    ? ""
                    : (reference.BlockTableRecord.GetObject(OpenMode.ForRead) as BlockTableRecord)?.Name ?? "";
                item["dynamic"] = reference.IsDynamicBlock;
                // What it was set to. A beam looks like a beam at any of its sizes, so the
                // geometry alone cannot tell a placed element from a wrong-sized one.
                item["properties"] = ReferenceProperties(reference);
            }
            found.Add(item);
        }
        tr.Commit();
        return found;
    }

    /// <summary>Zoom the current view to fit everything in the model space.</summary>
    /// <remarks>
    /// Synchronously, through the current view record, not by queueing a <c>ZOOM</c> command:
    /// the reason to zoom is usually to photograph the drawing next, and a queued command
    /// would let the picture be taken of the view as it was before. The extents are measured
    /// off the entities rather than read from <c>Extmin</c>/<c>Extmax</c>, which AutoCAD only
    /// refreshes on its own terms and which are therefore stale exactly when they matter.
    /// </remarks>
    public static JsonObject ZoomExtents(Document document)
    {
        var db = document.Database;
        using var docLock = document.LockDocument();
        using var tr = db.TransactionManager.StartTransaction();
        var model = (BlockTableRecord)tr.GetObject(
            SymbolUtilityServices.GetBlockModelSpaceId(db), OpenMode.ForRead);

        Extents3d? box = null;
        foreach (var id in model)
        {
            if (tr.GetObject(id, OpenMode.ForRead) is not Entity entity || entity.IsErased)
            {
                continue;
            }
            try
            {
                var extents = entity.GeometricExtents;
                box = box is null ? extents : Union(box.Value, extents);
            }
            catch (Autodesk.AutoCAD.Runtime.Exception)
            {
                // Some entities have no extents (a zero-length line, a text with no
                // glyphs). One of those must not cost the whole zoom.
            }
        }
        tr.Commit();

        if (box is null)
        {
            return new JsonObject { ["ok"] = false, ["message"] = "there is nothing to zoom to" };
        }

        var low = box.Value.MinPoint;
        var high = box.Value.MaxPoint;
        var width = Math.Max(high.X - low.X, 1.0);
        var height = Math.Max(high.Y - low.Y, 1.0);

        using var view = document.Editor.GetCurrentView();
        var centre = new Point2d((low.X + high.X) / 2.0, (low.Y + high.Y) / 2.0);

        // Fit the drawing inside the view's own shape, with a margin. Fitting the width and
        // letting the height follow, or the other way round, is what keeps a long thin plan
        // readable in a window that is not the same shape as it is.
        var aspect = view.Height > 1e-9 ? view.Width / view.Height : 1.0;
        const double margin = 1.1;
        if (width / height >= aspect)
        {
            view.Width = width * margin;
            view.Height = view.Width / aspect;
        }
        else
        {
            view.Height = height * margin;
            view.Width = view.Height * aspect;
        }
        view.CenterPoint = centre;
        document.Editor.SetCurrentView(view);

        return new JsonObject
        {
            ["ok"] = true,
            ["width"] = view.Width,
            ["height"] = view.Height,
            ["entities"] = model.Cast<ObjectId>().Count(),
        };
    }

    private static Extents3d Union(Extents3d a, Extents3d b) =>
        new(
            new Point3d(Math.Min(a.MinPoint.X, b.MinPoint.X), Math.Min(a.MinPoint.Y, b.MinPoint.Y), 0),
            new Point3d(Math.Max(a.MaxPoint.X, b.MaxPoint.X), Math.Max(a.MaxPoint.Y, b.MaxPoint.Y), 0));

    private static void LoadLinetype(Database db, string name, Transaction tr)
    {
        var linetypes = (LinetypeTable)tr.GetObject(db.LinetypeTableId, OpenMode.ForRead);
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
