using System.Text.Json.Nodes;
using AcadMcp.Core;

namespace AcadMcp.Core.Tests;

/// <summary>
/// Serialisation, with the emphasis on what happens when the payload is wrong.
/// The journal is read back out of a drawing file a person may have edited by
/// hand, so the reader must never raise and must never invent history.
/// </summary>
public class JournalJsonTests
{
    private static StepRecord SampleStep() => new()
    {
        State = StepState.Done,
        Op = "draw_room",
        Label = "draw_room \u65e0\u83cc\u5ba4",
        Note = "checked",
        Result = "area 11.76",
        Transaction = "AcadMcp: #1 draw_room",
        Mutated = true,
        Atomic = true,
        Executable = true,
        Accepted = true,
        Drifted = true,
        ObjectsBefore = ["2F0"],
        ObjectsAfter = ["2F1", "2F2"],
        Params = new JsonObject { ["name"] = "\u65e0\u83cc\u5ba4", ["thickness"] = 200 },
        Primitives =
        [
            new Primitive
            {
                Kind = PrimitiveKind.Polyline,
                Points = [0, 0, 4000, 0, 4000, 3000, 0, 3000],
                Closed = true,
                Layer = "A-WALL",
                Linetype = "DASHED",
            },
            new Primitive
            {
                Kind = PrimitiveKind.Text,
                Points = [2000, 1500],
                Text = "\u65e0\u83cc\u5ba4",
                Height = 350,
                Align = "center",
                Layer = "A-ROOM-NAME",
            },
            new Primitive { Kind = PrimitiveKind.Hatch, Pattern = "ANSI31", Scale = 40, Solid = false },
            new Primitive { Kind = PrimitiveKind.Circle, Points = [0, 0], Radius = 400 },
            new Primitive { Kind = PrimitiveKind.Dimension, Points = [0, 0, 6000, 0], Offset = -1600 },
            new Primitive
            {
                Kind = PrimitiveKind.Insert,
                Points = [4000, 3000],
                Block = "\u77e9\u5f62\u67f1",
                Source = @"D:\lib\columns.dwg",
                Rotation = 90,
                ScaleX = 1.5,
                ScaleY = 2,
                Layer = "S-COLUMN",
                Properties = new JsonObject { ["\u5c3a\u5bf8"] = "400x400", ["\u56fe\u5c42"] = 3 },
            },
        ],
        Timestamp = new DateTime(2026, 10, 15, 9, 30, 0, DateTimeKind.Local),
        DurationMs = 42,
    };

    // ---------------------------------------------------------------- round trip

    [Fact]
    public void RoundTripPreservesEveryField()
    {
        var journal = new Journal { Description = "floor plan" };
        journal.AddExecuted(SampleStep());

        var reloaded = JournalJson.FromJson(JournalJson.ToJson(journal));

        Assert.Equal("floor plan", reloaded.Description);
        var step = reloaded.At(1)!;
        Assert.Equal(StepState.Done, step.State);
        Assert.Equal("draw_room", step.Op);
        Assert.Equal("draw_room \u65e0\u83cc\u5ba4", step.Label);
        Assert.Equal("checked", step.Note);
        Assert.Equal("AcadMcp: #1 draw_room", step.Transaction);
        Assert.True(step.Accepted);
        Assert.True(step.Drifted);
        Assert.Equal(["2F0"], step.ObjectsBefore);
        Assert.Equal(["2F1", "2F2"], step.ObjectsAfter);
        Assert.Equal(42, step.DurationMs);
        Assert.Equal(new DateTime(2026, 10, 15, 9, 30, 0, DateTimeKind.Local), step.Timestamp);
    }

    [Fact]
    public void ChineseTextSurvivesTheRoundTrip_AndIsStoredAsAscii()
    {
        var journal = new Journal();
        journal.AddExecuted(SampleStep());

        var json = JournalJson.ToJson(journal);

        // The writer escapes non-ASCII deliberately, and the invariant worth
        // pinning is that the payload is pure ASCII: an Xrecord is a DXF string,
        // so ASCII-only content sidesteps codepage conversion inside the DWG.

        var step = JournalJson.FromJson(json).At(1)!;
        Assert.Equal("\u65e0\u83cc\u5ba4", step.Params!["name"]!.GetValue<string>());
        Assert.Equal("draw_room \u65e0\u83cc\u5ba4", step.Label);
        Assert.Equal("\u65e0\u83cc\u5ba4", step.Primitives[1].Text);
    }

    [Fact]
    public void RoundTripPreservesPrimitives()
    {
        var journal = new Journal();
        journal.AddExecuted(SampleStep());

        var step = JournalJson.FromJson(JournalJson.ToJson(journal)).At(1)!;

        Assert.Equal(6, step.Primitives.Count);
        Assert.Equal(PrimitiveKind.Polyline, step.Primitives[0].Kind);
        Assert.True(step.Primitives[0].Closed);
        Assert.Equal("DASHED", step.Primitives[0].Linetype);
        Assert.Equal(4, step.Primitives[0].PointCount);
        Assert.Equal(PrimitiveKind.Text, step.Primitives[1].Kind);
        Assert.Equal(350, step.Primitives[1].Height);
        Assert.Equal("center", step.Primitives[1].Align);
        Assert.Equal(40, step.Primitives[2].Scale);
        Assert.Equal(400, step.Primitives[3].Radius);
        Assert.Equal(-1600, step.Primitives[4].Offset);
    }

    [Fact]
    public void RoundTripPreservesAnInsert()
    {
        var journal = new Journal();
        journal.AddExecuted(SampleStep());

        var insert = JournalJson.FromJson(JournalJson.ToJson(journal)).At(1)!.Primitives[5];

        Assert.Equal(PrimitiveKind.Insert, insert.Kind);
        // The whole point of the insert travelling as a primitive: a replayed step
        // must be able to import the definition and set the size again, so every one
        // of these has to survive the wire and the Xrecord.
        Assert.Equal("\u77e9\u5f62\u67f1", insert.Block);
        Assert.Equal(@"D:\lib\columns.dwg", insert.Source);
        Assert.Equal((4000, 3000), insert.PointAt(0));
        Assert.Equal(90, insert.Rotation);
        Assert.Equal(1.5, insert.ScaleX);
        Assert.Equal(2, insert.ScaleY);
        Assert.Equal("S-COLUMN", insert.Layer);
        Assert.Equal("400x400", insert.Properties!["\u5c3a\u5bf8"]!.GetValue<string>());
        Assert.Equal(3, insert.Properties!["\u56fe\u5c42"]!.GetValue<int>());
    }

    [Fact]
    public void AnInsertDefaultsToAScaleOfOneAndNoProperties()
    {
        var primitive = new Primitive { Kind = PrimitiveKind.Insert, Block = "COLUMN" };

        Assert.Equal(1.0, primitive.ScaleX);
        Assert.Equal(1.0, primitive.ScaleY);
        Assert.Null(primitive.Properties);
        Assert.Null(primitive.Source);
        // Not the hatch pattern scale: the two are deliberately separate fields.
        Assert.Equal(1.0, primitive.Scale);
    }

    [Fact]
    public void AnEmptyJournalRoundTrips()
    {
        var reloaded = JournalJson.FromJson(JournalJson.ToJson(new Journal()));

        Assert.True(reloaded.IsEmpty);
    }

    [Fact]
    public void DescriptionSurvivesWithNoRecords()
    {
        var journal = new Journal { Description = "planned only" };

        Assert.Equal("planned only", JournalJson.FromJson(JournalJson.ToJson(journal)).Description);
    }

    // ---------------------------------------------------------------- tolerance

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not json at all")]
    [InlineData("{")]
    [InlineData("[]")]
    [InlineData("\"a string\"")]
    [InlineData("42")]
    [InlineData("null")]
    public void GarbageYieldsAnEmptyJournalAndNeverThrows(string? payload)
    {
        var journal = JournalJson.FromJson(payload);

        Assert.True(journal.IsEmpty);
    }

    [Fact]
    public void AJournalObjectWithNoRecordsArrayIsEmpty()
    {
        Assert.True(JournalJson.FromJson("{\"version\":1,\"description\":\"x\"}").IsEmpty);
    }

    [Fact]
    public void AMalformedRecordIsSkippedAndTheRestSurvive()
    {
        const string payload = """
            {"version":1,"records":[
              {"index":1,"state":"done","op":"first","objects_after":["H1"]},
              "this is not an object",
              {"index":3,"state":"done","op":"third","objects_after":["H3"]}
            ]}
            """;

        var journal = JournalJson.FromJson(payload);

        Assert.Equal(2, journal.Count);
        Assert.Equal("first", journal.At(1)!.Op);
        Assert.Equal("third", journal.At(2)!.Op);
    }

    [Fact]
    public void MissingFieldsFallBackToSafeDefaults()
    {
        var journal = JournalJson.FromJson("{\"records\":[{\"op\":\"bare\"}]}");

        var step = journal.At(1)!;
        Assert.Equal("bare", step.Op);
        Assert.True(step.Mutated);
        Assert.True(step.Atomic);
        Assert.True(step.Executable);
        Assert.False(step.Accepted);
        Assert.False(step.Drifted);
        Assert.Equal("", step.Transaction);
        Assert.Null(step.Params);
        Assert.Empty(step.Primitives);
    }

    [Fact]
    public void FieldsOfTheWrongTypeFallBackRatherThanThrow()
    {
        const string payload = """
            {"records":[{"op":123,"mutated":"not a bool","objects_after":"nope","duration_ms":"x"}]}
            """;

        var step = JournalJson.FromJson(payload).At(1)!;

        Assert.Equal("", step.Op);
        Assert.True(step.Mutated);
        Assert.Empty(step.ObjectsAfter);
        Assert.Equal(0, step.DurationMs);
    }

    [Fact]
    public void NonStringEntriesInHandleListsAreDropped()
    {
        var journal = JournalJson.FromJson("{\"records\":[{\"objects_after\":[\"2F1\",7,null,\"2F2\"]}]}");

        Assert.Equal(["2F1", "2F2"], journal.At(1)!.ObjectsAfter);
    }

    // ---------------------------------------------------------------- enum parsing

    [Theory]
    [InlineData("line", PrimitiveKind.Line)]
    [InlineData("Line", PrimitiveKind.Line)]
    [InlineData("POLYLINE", PrimitiveKind.Polyline)]
    [InlineData("mtext", PrimitiveKind.MText)]
    [InlineData("MText", PrimitiveKind.MText)]
    [InlineData("M_TEXT", PrimitiveKind.MText)]
    [InlineData("dimension", PrimitiveKind.Dimension)]
    // Not Line. Folding an unrecognised name into Line is what turned a sheet's `viewport`
    // — a kind the Python side really emits and this plugin has no verb for — into a
    // zero-length line at the origin, journalled and reported as a success.
    [InlineData("nonsense", PrimitiveKind.Unknown)]
    [InlineData("", PrimitiveKind.Unknown)]
    [InlineData("viewport", PrimitiveKind.Unknown)]
    public void KindParsingIsCaseInsensitiveAndRefusesAnUnknownName(string raw, PrimitiveKind expected)
    {
        Assert.Equal(expected, JournalJson.ReadKind(raw));
    }

    [Fact]
    public void AnUnknownKindKeepsTheNameItCouldNotRead()
    {
        var record = JournalJson.RecordFromNode(JsonNode.Parse(
            """{"op":"apply","primitives":[{"kind":"viewport","points":[0,0,1,1]}]}"""))!;

        Assert.Equal(PrimitiveKind.Unknown, record.Primitives[0].Kind);
        Assert.Equal("viewport", record.Primitives[0].UnknownKind);
        // And the refusal names it, rather than reporting a kind nobody sent.
        Assert.Contains("viewport", record.Primitives[0].UnbuildableReason());
    }

    [Fact]
    public void AKnownKindCarriesNoUnknownName()
    {
        var record = JournalJson.RecordFromNode(JsonNode.Parse(
            """{"op":"apply","primitives":[{"kind":"line","points":[0,0,1,1]}]}"""))!;

        Assert.Equal(PrimitiveKind.Line, record.Primitives[0].Kind);
        Assert.Null(record.Primitives[0].UnknownKind);
    }

    [Theory]
    [InlineData("planned", StepState.Planned)]
    [InlineData("PLANNED", StepState.Planned)]
    [InlineData("failed", StepState.Failed)]
    [InlineData("Failed", StepState.Failed)]
    [InlineData("done", StepState.Done)]
    public void StateParsingIsCaseInsensitive(string raw, StepState expected)
    {
        Assert.Equal(expected, JournalJson.ReadState(raw));
    }

    [Fact]
    public void AnUnreadableStateIsTreatedAsAppliedNotAsPlanned()
    {
        // Guessing "planned" here would let a rollback erase entities that the
        // drawing still needs, so the safe direction is to assume they exist.
        Assert.Equal(StepState.Done, JournalJson.ReadState("???"));
    }

    // ---------------------------------------------------------------- primitive behaviour

    [Fact]
    public void PointAtRefusesARunThatDoesNotReachIt()
    {
        var primitive = new Primitive { Points = [10, 20, 30, 40] };

        Assert.Equal((10, 20), primitive.PointAt(0));
        Assert.Equal((30, 40), primitive.PointAt(1));
        // (0, 0) here used to be the answer, which is how a line sent with one coordinate
        // became an entity drawn at the origin and journalled as a success. The message has
        // to name the primitive, because a caller reading it cannot see which one it was.
        var beyond = Assert.Throws<ArgumentOutOfRangeException>(() => primitive.PointAt(2));
        Assert.Contains("Line", beyond.Message);
        Assert.Throws<ArgumentOutOfRangeException>(() => primitive.PointAt(-1));
    }

    [Theory]
    [InlineData(PrimitiveKind.Line, 0, "needs at least 2 point(s)")]
    [InlineData(PrimitiveKind.Hatch, 4, "needs at least 3 point(s)")]
    [InlineData(PrimitiveKind.Circle, 0, "needs at least 1 point(s)")]
    public void AShortPrimitiveIsRefusedBeforeAnythingIsDrawn(
        PrimitiveKind kind, int coordinates, string expected)
    {
        var primitive = new Primitive { Kind = kind, Points = [.. Enumerable.Repeat(0.0, coordinates)] };

        Assert.Contains(expected, primitive.UnbuildableReason());
    }

    [Fact]
    public void AnOddCoordinateCountIsRefusedRatherThanHavingOneDropped()
    {
        // The vertex loop reads whole pairs, so the dangling coordinate vanished without a
        // word and the polyline came out one vertex short of what was asked for.
        var primitive = new Primitive { Kind = PrimitiveKind.Polyline, Points = [1, 2, 3] };

        Assert.Contains("odd number of coordinates", primitive.UnbuildableReason());
    }

    [Fact]
    public void ALongEnoughPrimitiveHasNothingToComplainAbout()
    {
        Assert.Empty(new Primitive { Kind = PrimitiveKind.Line, Points = [0, 0, 1, 1] }.UnbuildableReason());
        Assert.Empty(new Primitive { Kind = PrimitiveKind.Insert, Points = [0, 0] }.UnbuildableReason());
    }

    [Fact]
    public void PointCountCountsPairsNotNumbers()
    {
        Assert.Equal(2, new Primitive { Points = [0, 0, 1, 1] }.PointCount);
        Assert.Equal(0, new Primitive().PointCount);
    }

    [Fact]
    public void SummaryNamesTheKind()
    {
        Assert.Contains("polyline", new Primitive { Kind = PrimitiveKind.Polyline }.Summary);
        Assert.Contains("closed", new Primitive { Kind = PrimitiveKind.Polyline, Closed = true }.Summary);
        Assert.Contains("r=400", new Primitive { Kind = PrimitiveKind.Circle, Radius = 400 }.Summary);
        Assert.Contains("ANSI31", new Primitive { Kind = PrimitiveKind.Hatch }.Summary);
    }

    [Fact]
    public void SummaryTruncatesLongText()
    {
        var primitive = new Primitive { Kind = PrimitiveKind.Text, Text = new string('x', 60) };

        Assert.True(primitive.Summary.Length < 40);
    }

    // ---------------------------------------------------------------- record nodes

    [Fact]
    public void RecordToNodeAndBackIsLossless()
    {
        var node = JournalJson.RecordToNode(SampleStep());

        var record = JournalJson.RecordFromNode(node)!;

        Assert.Equal("draw_room", record.Op);
        Assert.Equal(["2F1", "2F2"], record.ObjectsAfter);
        Assert.Equal(6, record.Primitives.Count);
    }

    [Fact]
    public void RecordFromANonObjectIsNull()
    {
        Assert.Null(JournalJson.RecordFromNode(null));
        Assert.Null(JournalJson.RecordFromNode(JsonValue.Create("nope")));
    }

    [Fact]
    public void PrimitivesWithGarbageEntriesKeepTheGoodOnes()
    {
        const string payload = """
            {"records":[{"primitives":[{"kind":"line","points":[0,0,1,1]},"bad",{"kind":"text","text":"ok"}]}]}
            """;

        var step = JournalJson.FromJson(payload).At(1)!;

        Assert.Equal(2, step.Primitives.Count);
        Assert.Equal("ok", step.Primitives[1].Text);
    }

    [Fact]
    public void AJournalFromANewerBuildIsRefusedRatherThanReadWithDefaults()
    {
        // Every field reader here is tolerant on purpose, which is right for a hand-edited
        // file and exactly wrong for a newer one: a renamed or re-typed field would load as
        // its default, and the step would come back looking whole while meaning something this
        // build cannot interpret. The Python side refuses a newer payload for the same reason.
        const string payload = """
            {"version":99,"description":"from the future","records":[{"op":"draw_wall"}]}
            """;

        var journal = JournalJson.FromJson(payload);

        Assert.True(journal.IsEmpty);
        Assert.Equal("", journal.Description);
    }

    [Fact]
    public void AJournalFromThisBuildIsRead()
    {
        var journal = JournalJson.FromJson(
            $$"""{"version":{{JournalJson.Version}},"records":[{"op":"draw_wall"}]}""");

        Assert.Equal(1, journal.Count);
    }

    [Fact]
    public void TheWireSpellsPrimitiveKindsTheWayPythonComparesThem()
    {
        // The RPC step row hands these to the Python readers, which have no local journal to
        // fall back on when the plugin owns the drawing — and which compare kinds literally
        // (`kind != "text"`), so a PascalCase kind read as none of them. A count of primitives,
        // which is what this used to be, read as nothing at all.
        var journal = new Journal();
        journal.AddExecuted(new StepRecord
        {
            Op = "draw_wall",
            Primitives =
            [
                new Primitive { Kind = PrimitiveKind.Line, Points = [0, 0, 6000, 0], Layer = "A-WALL" },
                new Primitive { Kind = PrimitiveKind.Text, Points = [1, 2], Text = "ok" },
                new Primitive { Kind = PrimitiveKind.Insert, Points = [3, 4], Block = "TEST" },
            ],
        });

        var wire = JournalJson.PrimitivesForWire(journal.Records[0].Primitives);

        Assert.Equal(["line", "text", "insert"], wire.Select(p => p!["kind"]!.GetValue<string>()));
        // And the spelling Python sends back reads as the kind it names, so the two directions
        // cannot disagree about what a step holds.
        var readBack = JournalJson.RecordFromNode(
            new JsonObject { ["op"] = "x", ["primitives"] = wire.DeepClone() })!;
        Assert.Equal(journal.Records[0].Primitives.Select(p => p.Kind), readBack.Primitives.Select(p => p.Kind));
    }
}
