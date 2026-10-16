using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace AcadMcp.Core;

/// <summary>What a primitive is. The set mirrors the Python backend's drawing verbs.</summary>
[JsonConverter(typeof(JsonStringEnumConverter))]
public enum PrimitiveKind
{
    Line,
    Polyline,
    Circle,
    Arc,
    Text,
    MText,
    Hatch,
    Dimension,

    /// <summary>
    /// A reference to a block definition: one entity standing for a repeated element.
    /// </summary>
    /// <remarks>
    /// The definition itself is not a primitive. It belongs to the drawing the way a layer
    /// does — created once, idempotently, and owned by no particular step — because a step
    /// that owned it would take every later step's references down with it when it was
    /// rolled back.
    /// </remarks>
    Insert,

    /// <summary>A kind this build does not know how to draw. Never drawn.</summary>
    /// <remarks>
    /// Read from a name that matched nothing, and kept as a distinct member rather than
    /// folded into <see cref="Line"/> so that an unknown primitive is <b>refused</b> instead
    /// of being drawn as something it is not. That matters for a kind the Python side emits
    /// and this plugin has no verb for — a sheet's <c>viewport</c> is the real one — because
    /// folding it into a line draws a zero-length entity at the origin and calls the step a
    /// success. <see cref="Primitive.UnknownKind"/> carries the name, so the refusal can say
    /// which word was not understood.
    /// </remarks>
    Unknown,
}

/// <summary>
/// One drawable item — the unit a step is made of.
/// </summary>
/// <remarks>
/// This is the executable truth of a step. Geometry is computed on the Python
/// side (walls, punched openings, room areas); the plugin's job is to create
/// these in a transaction and to be able to create them again on replay, which
/// is why the resolved geometry travels with the step instead of only the
/// high-level arguments that produced it.
///
/// <see cref="Points"/> is flat (x1, y1, x2, y2, …) rather than nested pairs, so
/// the wire format stays a flat array of numbers in both directions.
/// </remarks>
public sealed record Primitive
{
    [JsonPropertyName("kind")]
    public PrimitiveKind Kind { get; init; } = PrimitiveKind.Line;

    /// <summary>
    /// The kind word as it arrived, when it named nothing this build can draw.
    /// </summary>
    /// <remarks>
    /// Persisted rather than kept in memory only, for the same reason the resolved geometry
    /// travels with the step: reopening the drawing and replaying it has to fail with the
    /// same explanation, not a vague one.
    /// </remarks>
    [JsonPropertyName("unknown_kind")]
    public string? UnknownKind { get; init; }

    /// <summary>Flat x, y pairs in millimetres. One pair for a centre, two for a line.</summary>
    [JsonPropertyName("points")]
    public IReadOnlyList<double> Points { get; init; } = [];

    [JsonPropertyName("closed")]
    public bool Closed { get; init; }

    [JsonPropertyName("radius")]
    public double Radius { get; init; }

    /// <summary>Arc start angle in degrees, counter-clockwise from +X.</summary>
    [JsonPropertyName("start_angle")]
    public double StartAngle { get; init; }

    [JsonPropertyName("end_angle")]
    public double EndAngle { get; init; }

    [JsonPropertyName("text")]
    public string Text { get; init; } = "";

    [JsonPropertyName("height")]
    public double Height { get; init; }

    /// <summary>MText wrap width; ignored by other kinds.</summary>
    [JsonPropertyName("width")]
    public double Width { get; init; }

    [JsonPropertyName("rotation")]
    public double Rotation { get; init; }

    /// <summary>Text alignment: left, center, right, top-center.</summary>
    [JsonPropertyName("align")]
    public string Align { get; init; } = "left";

    [JsonPropertyName("layer")]
    public string Layer { get; init; } = "0";

    [JsonPropertyName("linetype")]
    public string? Linetype { get; init; }

    [JsonPropertyName("pattern")]
    public string Pattern { get; init; } = "ANSI31";

    [JsonPropertyName("scale")]
    public double Scale { get; init; } = 1.0;

    /// <summary>Hatch pattern angle in degrees.</summary>
    [JsonPropertyName("angle")]
    public double Angle { get; init; }

    [JsonPropertyName("solid")]
    public bool Solid { get; init; }

    /// <summary>ACI colour index, or null to follow the layer.</summary>
    [JsonPropertyName("color")]
    public int? Color { get; init; }

    /// <summary>Dimension line offset from the measured run, in millimetres.</summary>
    [JsonPropertyName("offset")]
    public double Offset { get; init; }

    [JsonPropertyName("text_height")]
    public double TextHeight { get; init; }

    /// <summary>Name of the block to insert. Only meaningful for <see cref="PrimitiveKind.Insert"/>.</summary>
    [JsonPropertyName("block")]
    public string Block { get; init; } = "";

    /// <summary>
    /// Where to import the definition from when the drawing does not already have it: a
    /// library drawing, as an absolute path.
    /// </summary>
    /// <remarks>
    /// Carried on the primitive rather than configured once, so that replaying a step in a
    /// drawing that has never seen the library still works — the same reason the resolved
    /// geometry travels with the step.
    /// </remarks>
    [JsonPropertyName("source")]
    public string? Source { get; init; }

    /// <summary>Insertion scale. Separate from <see cref="Scale"/>, which is a hatch's pattern scale.</summary>
    [JsonPropertyName("scale_x")]
    public double ScaleX { get; init; } = 1.0;

    /// <inheritdoc cref="ScaleX"/>
    [JsonPropertyName("scale_y")]
    public double ScaleY { get; init; } = 1.0;

    /// <summary>
    /// Dynamic block properties to set on the reference: name to value.
    /// </summary>
    /// <remarks>
    /// This is how one block serves a range of sizes — a window block with a width lookup is
    /// set to 1800 rather than being a separate block. Values are checked against the block's
    /// allowed values, because a silently ignored setting is a wrong drawing that looks fine.
    /// </remarks>
    [JsonPropertyName("properties")]
    public JsonObject? Properties { get; init; }

    /// <summary>Number of (x, y) pairs in <see cref="Points"/>.</summary>
    [JsonIgnore]
    public int PointCount => Points.Count / 2;

    /// <summary>
    /// Point at <paramref name="index"/>, or a refusal when the array does not reach it.
    /// </summary>
    /// <remarks>
    /// This used to answer (0, 0) for an out-of-range index, which turned a malformed
    /// primitive — a line sent with one coordinate, a dimension sent with one point — into
    /// an entity drawn at the origin, journalled with a handle, and reported as a success.
    /// Nothing downstream could tell that drawing from a real one at the origin. Refusing is
    /// the only honest answer: the array is what the caller sent, so a short one is their
    /// bug and the message has to reach them.
    /// </remarks>
    public (double X, double Y) PointAt(int index)
    {
        var offset = index * 2;
        if (offset < 0 || offset + 1 >= Points.Count)
        {
            throw new ArgumentOutOfRangeException(
                nameof(index),
                $"a {Kind} primitive needs point {index}, but its points array holds "
                + $"{Points.Count} coordinate(s) — points are flat x, y pairs");
        }
        return (Points[offset], Points[offset + 1]);
    }

    /// <summary>How many points this kind cannot be drawn without.</summary>
    [JsonIgnore]
    public int MinimumPoints => Kind switch
    {
        PrimitiveKind.Line or PrimitiveKind.Polyline or PrimitiveKind.Dimension => 2,
        // A hatch boundary has to enclose an area; three points is the least that can.
        PrimitiveKind.Hatch => 3,
        PrimitiveKind.Unknown => 0,
        _ => 1,
    };

    /// <summary>
    /// Why this primitive cannot be drawn as it stands, or "" when it can.
    /// </summary>
    /// <remarks>
    /// Checked before anything is created, so a whole step fails with the caller's own
    /// primitive named rather than producing a wrong entity and a handle. A flat array with
    /// an odd length is refused here too: the dangling coordinate is silently dropped by the
    /// vertex loop otherwise, which is the same class of quiet wrong drawing.
    /// </remarks>
    public string UnbuildableReason()
    {
        if (Kind == PrimitiveKind.Unknown)
        {
            var name = string.IsNullOrWhiteSpace(UnknownKind) ? "(unnamed)" : $"'{UnknownKind}'";
            return $"this build cannot draw a {name} primitive, so nothing was drawn; it draws "
                + "lines, polylines, circles, arcs, text, mtext, hatches, dimensions and block "
                + "inserts";
        }
        if (Points.Count % 2 != 0)
        {
            return $"a {Kind} primitive has an odd number of coordinates ({Points.Count}); "
                + "points are flat x, y pairs, so the last one has no partner";
        }
        if (PointCount < MinimumPoints)
        {
            return $"a {Kind} primitive needs at least {MinimumPoints} point(s) and has "
                + $"{PointCount}; it would be drawn at the origin instead of where it was asked for";
        }
        return "";
    }

    /// <summary>A short human description, used in step rows and tooltips.</summary>
    [JsonIgnore]
    public string Summary => Kind switch
    {
        PrimitiveKind.Line => $"line ({PointCount} pts)",
        PrimitiveKind.Polyline => $"{(Closed ? "closed " : "")}polyline ({PointCount} pts)",
        PrimitiveKind.Circle => $"circle r={Radius:0.##}",
        PrimitiveKind.Arc => $"arc r={Radius:0.##}",
        PrimitiveKind.Text or PrimitiveKind.MText => $"text {Trim(Text)}",
        PrimitiveKind.Hatch => $"hatch {Pattern}",
        PrimitiveKind.Dimension => "dimension",
        PrimitiveKind.Insert => Properties is { Count: > 0 }
            ? $"insert {Block} ({Properties.Count} property/ies)"
            : $"insert {Block}",
        _ => Kind.ToString(),
    };

    private static string Trim(string value) =>
        value.Length <= 24 ? value : value[..21] + "\u2026";
}
