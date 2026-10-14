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

    /// <summary>Number of (x, y) pairs in <see cref="Points"/>.</summary>
    [JsonIgnore]
    public int PointCount => Points.Count / 2;

    /// <summary>Point at <paramref name="index"/>, or (0, 0) when out of range.</summary>
    public (double X, double Y) PointAt(int index)
    {
        var offset = index * 2;
        if (offset < 0 || offset + 1 >= Points.Count)
        {
            return (0, 0);
        }
        return (Points[offset], Points[offset + 1]);
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
        _ => Kind.ToString(),
    };

    private static string Trim(string value) =>
        value.Length <= 24 ? value : value[..21] + "\u2026";
}
