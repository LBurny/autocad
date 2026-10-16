using System.Globalization;
using System.Text.Json.Nodes;
using AcadMcp.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

namespace AcadMcp.Plugin;

/// <summary>
/// What a primitive needs from the drawing that the primitive itself cannot know.
/// </summary>
/// <remarks>
/// Bundled into one type rather than passed as loose arguments so that the next
/// thing a primitive needs — a resolved dimstyle, say — adds a field here instead
/// of changing <see cref="PrimitiveBuilder.Build"/>'s signature and every call site.
/// </remarks>
public sealed record BuildContext
{
    public required Database Database { get; init; }

    /// <summary>The CJK-capable text style, from <see cref="TextStyles.Ensure"/>.</summary>
    public ObjectId TextStyle { get; init; }

    /// <summary>
    /// The definition an <c>Insert</c> refers to. Resolved by the caller, which is
    /// the only party that can import it — see <see cref="StepEngine.EnsureBlock"/>.
    /// </summary>
    public ObjectId BlockDefinition { get; init; }
}

/// <summary>
/// Turns a <see cref="Primitive"/> from the Python side into a real AutoCAD
/// entity.
/// </summary>
/// <remarks>
/// Geometry is computed in Python and arrives here already resolved, so this
/// class holds no drafting logic at all — it is a translator, and deliberately a
/// dull one. That is what lets the plugin replay a step without the MCP process
/// running.
///
/// Angles cross the wire in degrees, counter-clockwise from +X, because that is
/// what a person reading a plan expects. AutoCAD's constructors want radians, so
/// every angle in this file goes through <see cref="Radians"/>.
/// </remarks>
public static class PrimitiveBuilder
{
    /// <summary>
    /// Create the entity for a primitive. It is not yet in the database.
    /// </summary>
    public static Entity Build(Primitive primitive, BuildContext context)
    {
        // Before anything is created: a primitive that cannot be drawn as asked has to fail
        // here, where the caller's own error can be named, rather than becoming a
        // right-looking entity somewhere nobody meant it to be.
        var unbuildable = primitive.UnbuildableReason();
        if (unbuildable.Length > 0)
        {
            throw new InvalidOperationException(unbuildable);
        }

        var entity = primitive.Kind switch
        {
            PrimitiveKind.Line => BuildLine(primitive),
            PrimitiveKind.Polyline => BuildPolyline(primitive),
            PrimitiveKind.Circle => BuildCircle(primitive),
            PrimitiveKind.Arc => BuildArc(primitive),
            PrimitiveKind.Text => BuildText(primitive, context.TextStyle),
            PrimitiveKind.MText => BuildMText(primitive, context.TextStyle),
            PrimitiveKind.Hatch => BuildHatch(primitive),
            PrimitiveKind.Dimension => BuildDimension(primitive, context.Database),
            PrimitiveKind.Insert => BuildInsert(primitive, context),
            // Unreachable: UnbuildableReason above already refuses Unknown, naming the kind.
            // Kept so that adding a member to the enum without a builder is a compile-time
            // gap rather than a silently undrawn primitive.
            _ => throw new ArgumentOutOfRangeException(
                nameof(primitive), primitive.Kind, "unsupported primitive kind"),
        };

        ApplyStyle(entity, primitive);
        return entity;
    }

    /// <summary>
    /// Work that can only happen once the entity is in the database.
    /// </summary>
    /// <remarks>
    /// A hatch has to be appended to a block before it can be evaluated; doing it
    /// earlier either throws or silently produces an empty hatch. A dynamic block
    /// reference is the same shape of problem: its properties are resolved against
    /// the definition, and the definition settles only once the reference is in the
    /// drawing.
    /// </remarks>
    public static void Finish(Entity entity, Primitive primitive)
    {
        switch (entity)
        {
            case Hatch hatch:
                hatch.EvaluateHatch(false);
                break;
            case BlockReference reference:
                FinishInsert(reference, primitive);
                break;
        }
    }

    // ---------------------------------------------------------------- geometry

    private static Entity BuildLine(Primitive p) =>
        new Line(Point(p, 0), Point(p, 1));

    private static Entity BuildPolyline(Primitive p)
    {
        var polyline = new Polyline();
        for (var i = 0; i < p.PointCount; i++)
        {
            var (x, y) = p.PointAt(i);
            // bulge 0 and zero widths: a plain straight segment between vertices.
            polyline.AddVertexAt(i, new Point2d(x, y), 0.0, 0.0, 0.0);
        }
        polyline.Closed = p.Closed;
        return polyline;
    }

    private static Entity BuildCircle(Primitive p) =>
        new Circle(Point(p, 0), Vector3d.ZAxis, p.Radius);

    private static Entity BuildArc(Primitive p) =>
        new Arc(Point(p, 0), p.Radius, Radians(p.StartAngle), Radians(p.EndAngle));

    private static Entity BuildText(Primitive p, ObjectId textStyle)
    {
        var position = Point(p, 0);
        var height = p.Height > 0 ? p.Height : 2.5;
        var text = new DBText
        {
            TextString = p.Text,
            Height = height,
            Rotation = Radians(p.Rotation),
            Position = position,
        };

        if (!textStyle.IsNull)
        {
            // Repointing a text's style can reset properties on it, so the style
            // goes on before the height is re-applied — and before the alignment
            // modes below, for the same reason.
            text.TextStyleId = textStyle;
            text.Height = height;
        }

        var (horizontal, vertical) = AlignmentFor(p.Align);
        if (horizontal != TextHorizontalMode.TextLeft)
        {
            // The alignment point is only honoured for a non-left mode, and
            // setting it while the mode is still left throws.
            text.HorizontalMode = horizontal;
            text.VerticalMode = vertical;
            text.AlignmentPoint = position;
        }
        return text;
    }

    private static Entity BuildMText(Primitive p, ObjectId textStyle)
    {
        var position = Point(p, 0);
        var height = p.Height > 0 ? p.Height : 2.5;
        var text = new MText
        {
            Contents = p.Text,
            TextHeight = height,
            Location = position,
            // A zero-width mtext is rejected by AutoCAD, so fall back to the same
            // generous default the DXF backend uses.
            Width = p.Width > 0 ? p.Width : 6000.0,
            // Rotation was silently dropped here, so a rotated mtext arrived horizontal while
            // the same journal written to DXF came out turned.
            Rotation = Radians(p.Rotation),
            Attachment = AttachmentForMText(p.Align),
        };

        if (!textStyle.IsNull)
        {
            text.TextStyleId = textStyle;
            text.TextHeight = height;
        }
        return text;
    }

    private static Entity BuildHatch(Primitive p)
    {
        var hatch = new Hatch();
        hatch.SetHatchPattern(HatchPatternType.PreDefined, p.Solid ? "SOLID" : p.Pattern);
        if (!p.Solid)
        {
            hatch.PatternScale = p.Scale > 0 ? p.Scale : 1.0;
            hatch.PatternAngle = Radians(p.Angle);
        }

        var vertices = new Point2dCollection();
        for (var i = 0; i < p.PointCount; i++)
        {
            var (x, y) = p.PointAt(i);
            vertices.Add(new Point2d(x, y));
        }
        // An empty bulge collection means "all segments straight". This overload
        // takes the boundary as points, so no temporary boundary entity is
        // needed — unlike the COM backend, which has to create one and delete it.
        hatch.AppendLoop(HatchLoopTypes.External, vertices, new DoubleCollection());
        return hatch;
    }

    private static Entity BuildDimension(Primitive p, Database database)
    {
        var (x1, y1) = p.PointAt(0);
        var (x2, y2) = p.PointAt(1);

        // The caller gives the offset sideways from the measured run; the
        // dimension line point is the midpoint pushed out along the run's normal.
        var dx = x2 - x1;
        var dy = y2 - y1;
        var length = Math.Sqrt(dx * dx + dy * dy);
        var (nx, ny) = length > 0 ? (-dy / length, dx / length) : (0.0, 1.0);
        var mid = ((x1 + x2) / 2.0, (y1 + y2) / 2.0);

        var dimension = new AlignedDimension
        {
            XLine1Point = new Point3d(x1, y1, 0),
            XLine2Point = new Point3d(x2, y2, 0),
            DimLinePoint = new Point3d(mid.Item1 + nx * p.Offset, mid.Item2 + ny * p.Offset, 0),
            DimensionText = "",
            DimensionStyle = database.Dimstyle,
        };

        // The text height travels with the primitive as a *model* height — the caller scaled
        // it by the sheet's plot scale — and the dimension style's own dimtxt is a paper
        // height. Dropping it, which this did, left every dimension on a 1:100 plan with text
        // about a hundredth of the size asked for, and made the same journal plot differently
        // from the DXF backend, which overrides dimtxt for exactly this reason. A per-entity
        // override rather than a change to the style: the style is document state shared with
        // the user's own dimensions.
        if (p.TextHeight > 0)
        {
            dimension.Dimtxt = p.TextHeight;
        }
        return dimension;
    }

    private static Entity BuildInsert(Primitive p, BuildContext context)
    {
        if (string.IsNullOrWhiteSpace(p.Block))
        {
            throw new InvalidOperationException("an insert needs a block name");
        }
        if (context.BlockDefinition.IsNull)
        {
            throw new InvalidOperationException(
                $"no definition for block '{p.Block}' was resolved for this insert");
        }
        if (p.ScaleX == 0.0 || p.ScaleY == 0.0)
        {
            // A zero scale collapses the block onto its insertion point: the
            // reference exists and owns nothing visible, which reads to the user as
            // "the plugin lost my column".
            throw new InvalidOperationException(
                $"inserting '{p.Block}' at a zero scale would be invisible");
        }

        return new BlockReference(Point(p, 0), context.BlockDefinition)
        {
            Rotation = Radians(p.Rotation),
            ScaleFactors = new Scale3d(p.ScaleX, p.ScaleY, 1.0),
        };
    }

    // ---------------------------------------------------------------- dynamic blocks

    /// <summary>Apply the requested dynamic block properties, or say why not.</summary>
    /// <remarks>
    /// <para>
    /// Every refusal here is deliberate, and none of them is silent. A dynamic block
    /// serves a whole range of sizes through these properties, so a setting that
    /// quietly does nothing yields a drawing that looks finished and is the wrong
    /// size — worse than an error, because nothing downstream can tell. So an unknown
    /// name, a property the current visibility state hides, a read-only property, and
    /// a value outside the published set all throw.
    /// </para>
    /// <para>
    /// The messages list what could have been asked for instead, which in practice is
    /// enough to fix the call in one round.
    /// </para>
    /// </remarks>
    private static void FinishInsert(BlockReference reference, Primitive p)
    {
        if (p.Properties is not { Count: > 0 })
        {
            return;
        }

        foreach (var (name, requested) in p.Properties)
        {
            // Re-read inside the loop: setting a visibility state adds and removes
            // other properties, so the collection from the previous iteration is no
            // longer the one to search.
            var properties = DynamicProperties(reference);
            var property = properties.FirstOrDefault(
                candidate => string.Equals(candidate.PropertyName, name, StringComparison.OrdinalIgnoreCase));

            if (property is null)
            {
                throw new InvalidOperationException(
                    $"block '{reference.Name}' has no property '{name}'; it has {Names(properties)}");
            }
            if (!property.VisibleInCurrentVisibilityState)
            {
                throw new InvalidOperationException(
                    $"property '{property.PropertyName}' of block '{reference.Name}' is hidden by its "
                    + "current visibility state; set that property first. Visible now: "
                    + Names(properties.Where(prop => prop.VisibleInCurrentVisibilityState)));
            }
            if (property.ReadOnly)
            {
                throw new InvalidOperationException(
                    $"property '{property.PropertyName}' of block '{reference.Name}' is read-only");
            }

            property.Value = ValueFor(property, requested, reference.Name);
        }
    }

    private static List<DynamicBlockReferenceProperty> DynamicProperties(BlockReference reference)
    {
        if (!reference.IsDynamicBlock)
        {
            return [];
        }

        // Indexed rather than foreach: this collection's enumerator is the non-generic
        // one, so foreach would hand back object.
        var collection = reference.DynamicBlockReferencePropertyCollection;
        var properties = new List<DynamicBlockReferenceProperty>(collection.Count);
        for (var i = 0; i < collection.Count; i++)
        {
            properties.Add(collection[i]);
        }
        return properties;
    }

    /// <summary>The value to assign, validated against what the block publishes.</summary>
    /// <remarks>
    /// When the property publishes an allowed set, the matching member of that set is
    /// assigned rather than a converted copy of the request: the published object is
    /// already the type AutoCAD wants (a short for one block, a long for the next), so
    /// this validates and converts in one step and cannot guess the type wrong.
    /// </remarks>
    private static object ValueFor(
        DynamicBlockReferenceProperty property, JsonNode? requested, string block)
    {
        var offered = (property.GetAllowedValues() ?? []).Where(value => value is not null).ToList();
        if (offered.Count > 0)
        {
            foreach (var value in offered)
            {
                if (Matches(value!, requested))
                {
                    return value!;
                }
            }
            throw new InvalidOperationException(
                $"'{Text(requested)}' is not an allowed value for '{property.PropertyName}' of block "
                + $"'{block}'; allowed: {Describe(offered)}");
        }

        // No published set, so the type of the current value is the only thing that
        // says whether this property wants 300 or "300".
        return property.Value switch
        {
            string => Text(requested),
            bool => Flag(requested),
            short => (short)Integer(requested, property, block),
            int => (int)Integer(requested, property, block),
            long => Integer(requested, property, block),
            double => Number(requested, property, block),
            null => throw new InvalidOperationException(
                $"property '{property.PropertyName}' of block '{block}' has no value to match a "
                + $"type against"),
            var current => throw new InvalidOperationException(
                $"property '{property.PropertyName}' of block '{block}' is of an unsupported type "
                + $"({current.GetType().Name})"),
        };
    }

    private static bool Matches(object allowed, JsonNode? requested) => allowed switch
    {
        string text => string.Equals(text, Text(requested), StringComparison.Ordinal),
        bool flag => Flag(requested) == flag,
        _ => requested is JsonValue value && value.TryGetValue<double>(out var number)
            && Math.Abs(System.Convert.ToDouble(allowed, CultureInfo.InvariantCulture) - number) < 1e-9,
    };

    private static double Number(JsonNode? node, DynamicBlockReferenceProperty property, string block) =>
        node is JsonValue value && value.TryGetValue<double>(out var number)
            ? number
            : throw new InvalidOperationException(
                $"property '{property.PropertyName}' of block '{block}' takes a number, "
                + $"not '{Text(node)}'");

    /// <summary>A number for an integral property, refusing to truncate silently.</summary>
    private static long Integer(JsonNode? node, DynamicBlockReferenceProperty property, string block)
    {
        var number = Number(node, property, block);
        if (Math.Abs(number - Math.Round(number)) > 1e-9)
        {
            throw new InvalidOperationException(
                $"property '{property.PropertyName}' of block '{block}' takes a whole number, "
                + $"not {number.ToString(CultureInfo.InvariantCulture)}");
        }
        return (long)Math.Round(number);
    }

    private static bool Flag(JsonNode? node)
    {
        if (node is JsonValue value)
        {
            if (value.TryGetValue<bool>(out var flag))
            {
                return flag;
            }
            // "true" from a form field or a JSON string is still a truth value.
            if (value.TryGetValue<string>(out var text) && bool.TryParse(text, out var parsed))
            {
                return parsed;
            }
        }
        return false;
    }

    /// <summary>The requested value as text, for messages and string-typed properties.</summary>
    private static string Text(JsonNode? node) => node is null ? "" : node.ToJsonString().Trim('"');

    private static string Names(IEnumerable<DynamicBlockReferenceProperty> properties)
    {
        var names = string.Join(", ", properties.Select(prop => $"'{prop.PropertyName}'"));
        return names.Length > 0 ? names : "none";
    }

    private static string Describe(IEnumerable<object> values) =>
        string.Join(", ", values.Select(value => value is string text
            ? $"'{text}'"
            // Invariant: the reader of this message is a caller comparing it against the
            // values it sent, and a comma decimal separator would show it something else.
            : Convert.ToString(value, CultureInfo.InvariantCulture)));

    // ---------------------------------------------------------------- helpers

    /// <summary>The alignment words this backend can put on a text.</summary>
    private const string TextAlignments = "'left', 'center', 'right', 'top-center'";

    /// <summary>
    /// Alignment modes for an align word. Refuses anything it cannot draw as asked.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The three left-anchored vertical modes are refused rather than drawn. A DBText honours
    /// its vertical mode only when the horizontal mode is centred or right — the alignment
    /// point is not used at all for a left text — so accepting "top-left" would draw
    /// baseline-left text and say nothing, which is the silently-wrong drawing this file
    /// refuses everywhere else. The drawing backend this journal also runs on <em>can</em>
    /// express all six, which is exactly why the difference has to be said out loud instead
    /// of showing up as two different plans.
    /// </para>
    /// <para>
    /// An unknown word is refused too. It used to fall through to left-aligned baseline text,
    /// so a typo was indistinguishable from a request.
    /// </para>
    /// </remarks>
    private static (TextHorizontalMode, TextVerticalMode) AlignmentFor(string align) => align switch
    {
        "left" => (TextHorizontalMode.TextLeft, TextVerticalMode.TextBase),
        "center" => (TextHorizontalMode.TextCenter, TextVerticalMode.TextVerticalMid),
        "right" => (TextHorizontalMode.TextRight, TextVerticalMode.TextVerticalMid),
        "top-center" => (TextHorizontalMode.TextCenter, TextVerticalMode.TextTop),
        "middle-left" or "top-left" or "bottom-left" => throw new InvalidOperationException(
            $"this backend cannot draw a '{align}' text: AutoCAD ignores a text's vertical "
            + $"alignment unless it is centred or right, so it would come out baseline-left and "
            + $"say nothing. Use one of {TextAlignments}, or place it at the baseline you want"),
        _ => throw new InvalidOperationException(
            $"'{align}' is not a text alignment; it is one of {TextAlignments}"),
    };

    /// <summary>
    /// MTEXT attachment point for an align word, on the same vocabulary as
    /// <see cref="AlignmentFor"/> so one word means one thing across both kinds.
    /// </summary>
    /// <remarks>
    /// MTEXT carries the whole placement in this one enum, where TEXT splits it across two
    /// modes, so the map is not the same table — but it is the same vocabulary. "left" lands
    /// on the bottom-left corner the DXF backend also uses, so the same journal drawn twice
    /// still agrees.
    /// </remarks>
    private static AttachmentPoint AttachmentForMText(string align) => align switch
    {
        "left" => AttachmentPoint.BottomLeft,
        "center" => AttachmentPoint.MiddleCenter,
        "top-center" => AttachmentPoint.TopCenter,
        _ => throw new InvalidOperationException(
            $"'{align}' is not an mtext alignment; it is one of {TextAlignments}"),
    };

    private static Point3d Point(Primitive p, int index)
    {
        var (x, y) = p.PointAt(index);
        return new Point3d(x, y, 0);
    }

    private static double Radians(double degrees) => degrees * Math.PI / 180.0;

    private static void ApplyStyle(Entity entity, Primitive p)
    {
        if (!string.IsNullOrWhiteSpace(p.Layer))
        {
            entity.Layer = p.Layer;
        }
        if (!string.IsNullOrWhiteSpace(p.Linetype))
        {
            // Silently ignored when the linetype is not loaded in this drawing:
            // a missing dashed pattern is not worth losing the geometry over.
            try
            {
                entity.Linetype = p.Linetype;
            }
            catch (Autodesk.AutoCAD.Runtime.Exception)
            {
            }
        }
        if (p.Color is { } color)
        {
            // 0 (by block) and 256 (by layer) are AutoCAD's own two "ask something else"
            // values, and both are what leaving the property alone already means. Anything
            // else outside 1..255 is not an ACI index at all, and quietly ignoring it would
            // hand back an entity in the layer's colour while the caller believed otherwise.
            if (color is < 0 or > 256)
            {
                throw new InvalidOperationException(
                    $"color {color} is not an ACI colour index; they run from 1 to 255, "
                    + "with 0 for by-block and 256 for by-layer");
            }
            if (color is > 0 and < 256)
            {
                entity.ColorIndex = color;
            }
        }
    }
}
