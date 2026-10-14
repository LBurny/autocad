using System.Text.Json.Nodes;
using AcadMcp.Core;
using Xunit;

namespace AcadMcp.Core.Tests;

/// <summary>
/// Checks on the two properties the store and the incremental writer depend on.
/// </summary>
/// <remarks>
/// <para>
/// The first is that a step's <em>number</em> and its <em>position</em> always agree. The
/// journal is reached positionally (<see cref="Journal.At"/>) while the record carries its
/// own index, and several operations compare that index instead — so if a damaged load can
/// leave the two disagreeing, a caller asking for step 4 can be handed step 5.
/// </para>
/// <para>
/// The second is that a mutator never changes a record in place. Writing only what changed
/// compares record instances, so an in-place change would be invisible and silently skip a
/// write. Both properties are cheap to state and expensive to discover in a drawing.
/// </para>
/// </remarks>
public class JournalLoadTests
{
    private static Journal JournalOf(int steps)
    {
        var journal = new Journal();
        for (var i = 1; i <= steps; i++)
        {
            journal.AddExecuted(new StepRecord { Op = $"op{i}", State = StepState.Done });
        }
        return journal;
    }

    private static string Payload(string op) =>
        JournalJson.RecordToNode(new StepRecord { Op = op, State = StepState.Done }).ToJsonString();

    [Fact]
    public void LoadingNumbersRecordsByPosition()
    {
        var journal = new Journal();
        var unreadable = JournalJson.AppendOrdered(
            journal,
            [Payload("first"), Payload("second"), Payload("third")]);

        Assert.Equal(0, unreadable);
        Assert.Equal([1, 2, 3], journal.Records.Select(r => r.Index));
        Assert.Equal("second", journal.At(2)!.Op);
    }

    [Fact]
    public void ARecordThatCannotBeReadIsCountedAndDoesNotLeaveAHole()
    {
        // The middle record is gone: a truncated chunk chain, or a hand-edited
        // dictionary. The two survivors must still be numbered 1 and 2, because that is
        // what a save will write them back as — and because At(2) has to mean the same
        // step the row for 2 shows.
        var journal = new Journal();
        var unreadable = JournalJson.AppendOrdered(
            journal,
            [Payload("first"), null, Payload("third")]);

        Assert.Equal(1, unreadable);
        Assert.Equal(2, journal.Count);
        Assert.Equal([1, 2], journal.Records.Select(r => r.Index));
        Assert.Equal("first", journal.At(1)!.Op);
        Assert.Equal("third", journal.At(2)!.Op);
    }

    [Fact]
    public void GarbagePayloadsAreCountedTooAndDoNotStopTheRest()
    {
        var journal = new Journal();
        var unreadable = JournalJson.AppendOrdered(
            journal,
            [Payload("first"), "{not json at all", "[]", Payload("last")]);

        Assert.Equal(2, unreadable);
        Assert.Equal(["first", "last"], journal.Records.Select(r => r.Op));
    }

    [Fact]
    public void EveryRecordCanBeFetchedByItsOwnIndex()
    {
        // The invariant behind every "roll back to step N" in the panel and the tools.
        var journal = new Journal();
        JournalJson.AppendOrdered(journal, [Payload("a"), null, Payload("c"), Payload("d")]);

        foreach (var record in journal.Records)
        {
            Assert.Same(record, journal.At(record.Index));
        }
    }

    [Fact]
    public void TheUnreadableCountStartsAtZero()
    {
        Assert.Equal(0, new Journal().Unreadable);
    }

    [Fact]
    public void AnEmptyOrWhitespacePayloadIsUnreadableRatherThanAStep()
    {
        var journal = new Journal();
        var unreadable = JournalJson.AppendOrdered(journal, [Payload("a"), "", "   "]);

        Assert.Equal(2, unreadable);
        Assert.Single(journal.Records);
    }
}

/// <summary>
/// Pins the property the incremental writer relies on: a record that changed is a
/// different instance, and a record that did not change is the same instance.
/// </summary>
/// <remarks>
/// <see cref="JournalStore"/>-style saving compares instances instead of being told which
/// records changed, because the alternative — remembering, at every call site, which
/// indices a mutation touched — is a silent-corruption bug waiting to happen. That trade
/// only holds while records are immutable and every mutator replaces them, so it is
/// asserted here rather than assumed.
/// </remarks>
public class JournalMutationTests
{
    private static JsonArray Snapshot(Journal journal) =>
        new(journal.Records.Select(r => JournalJson.RecordToNode(r)).ToArray<JsonNode?>());

    /// <summary>Run one mutation and assert no record changed without changing instance.</summary>
    private static void AssertNeverMutatedInPlace(Journal journal, Action<Journal> mutate)
    {
        var before = journal.Records.Select(r => (Record: r, Json: JournalJson.RecordToNode(r).ToJsonString()))
            .ToList();

        mutate(journal);

        var after = journal.Records.Select(r => (Record: r, Json: JournalJson.RecordToNode(r).ToJsonString()))
            .ToList();

        // Only the positions that exist in both, since a mutation may add or remove: what
        // is being asserted is about one position surviving unchanged. Where a record was
        // removed or inserted the positions shift, and comparing across the shift would
        // say nothing.
        var shared = Math.Min(before.Count, after.Count);
        for (var i = 0; i < shared; i++)
        {
            if (!ReferenceEquals(before[i].Record, after[i].Record))
            {
                continue;
            }
            Assert.True(
                before[i].Json == after[i].Json,
                $"position {i + 1} kept the same record instance but its content changed, "
                + "so a writer that diffs by reference would skip it");
        }
    }

    [Fact]
    public void NoMutatorChangesARecordInPlace()
    {
        AssertNeverMutatedInPlace(JournalOf(), j => j.AddExecuted(new StepRecord { Op = "new" }));
        AssertNeverMutatedInPlace(JournalOf(), j => j.Replace(2, j.At(2)! with { Label = "renamed" }));
        AssertNeverMutatedInPlace(JournalOf(), j => j.SetAccepted(2, true));
        AssertNeverMutatedInPlace(JournalOf(), j => j.SetNote(2, "a note"));
        AssertNeverMutatedInPlace(JournalOf(), j => j.MarkDrifted(2));
        AssertNeverMutatedInPlace(JournalOf(), j => j.ReplacePrimitives(2, []));
        AssertNeverMutatedInPlace(JournalOf(), j => j.Rewind(2));
        AssertNeverMutatedInPlace(JournalOf(), j => j.PlanReject(3));
        AssertNeverMutatedInPlace(
            JournalOf(),
            j => j.Update(2, new JsonObject { ["note"] = "patched" }));
        AssertNeverMutatedInPlace(
            JournalOf(),
            j => j.SetPlan("plan", [new StepRecord { Op = "planned" }]));
        AssertNeverMutatedInPlace(JournalOf(), j => j.DropPlanned());
    }

    [Fact]
    public void EditingOneStepLeavesTheOthersAlone()
    {
        // The other half of the bargain: unchanged steps must keep their instance, or the
        // writer would rewrite the whole journal every time and gain nothing.
        var journal = JournalOf(4);
        var before = journal.Records.ToList();

        journal.SetNote(2, "patched");

        Assert.Same(before[0], journal.At(1));
        Assert.NotSame(before[1], journal.At(2));
        Assert.Same(before[2], journal.At(3));
        Assert.Same(before[3], journal.At(4));
    }

    [Fact]
    public void RollingBackLeavesTheSurvivingStepAlone()
    {
        var journal = JournalOf(4);
        var first = journal.At(1);

        journal.Rewind(1);

        Assert.Same(first, journal.At(1));
        Assert.All(journal.Records.Skip(1), r => Assert.Equal(StepState.Planned, r.State));
    }

    private static Journal JournalOf(int steps = 3)
    {
        var journal = new Journal();
        for (var i = 1; i <= steps; i++)
        {
            journal.AddExecuted(new StepRecord { Op = $"op{i}", State = StepState.Done });
        }
        return journal;
    }
}
