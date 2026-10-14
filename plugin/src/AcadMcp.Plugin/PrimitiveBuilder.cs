using AcadMcp.Core;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;

namespace AcadMcp.Plugin;

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
    /// <param name="textStyle">
    /// The CJK-capable text style, from <see cref="TextStyles.Ensure"/>. Passed in
    /// rather than looked up so the caller can hoist the lookup out of its loop.
    /// </param>
    public static Entity Build(Primitive primitive, Database database, ObjectId textStyle)
    {
        var entity = primitive.Kind switch
        {
            PrimitiveKind.Line => BuildLine(primitive),
            PrimitiveKind.Polyline => BuildPolyline(primitive),
            PrimitiveKind.Circle => BuildCircle(primitive),
            PrimitiveKind.Arc => BuildArc(primitive),
            PrimitiveKind.Text => BuildText(primitive, textStyle),
            PrimitiveKind.MText => BuildMText(primitive, textStyle),
            PrimitiveKind.Hatch => BuildHatch(primitive),
            PrimitiveKind.Dimension => BuildDimension(primitive, database),
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
    /// earlier either throws or silently produces an empty hatch.
    /// </remarks>
    public static void Finish(Entity entity)
    {
        if (entity is Hatch hatch)
        {
            hatch.EvaluateHatch(false);
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
            Attachment = p.Align == "left" ? AttachmentPoint.TopLeft : AttachmentPoint.MiddleCenter,
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
        return dimension;
    }

    // ---------------------------------------------------------------- helpers

    private static (TextHorizontalMode, TextVerticalMode) AlignmentFor(string align) => align switch
    {
        "center" => (TextHorizontalMode.TextCenter, TextVerticalMode.TextVerticalMid),
        "right" => (TextHorizontalMode.TextRight, TextVerticalMode.TextVerticalMid),
        "middle-left" => (TextHorizontalMode.TextLeft, TextVerticalMode.TextVerticalMid),
        "top-center" => (TextHorizontalMode.TextCenter, TextVerticalMode.TextTop),
        "top-left" => (TextHorizontalMode.TextLeft, TextVerticalMode.TextTop),
        "bottom-left" => (TextHorizontalMode.TextLeft, TextVerticalMode.TextBottom),
        _ => (TextHorizontalMode.TextLeft, TextVerticalMode.TextBase),
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
    }
}
