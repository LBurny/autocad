using AcadMcp.Core;

namespace AcadMcp.Core.Tests;

/// <summary>
/// Plan management, rollback arithmetic and history edits. Nothing here needs
/// AutoCAD: the journal is the CAD-free half of the plugin by construction.
/// </summary>
public class JournalTests
{
    private static StepRecord Done(params string[] handles) => new()
    {
        State = StepState.Done,
        Op = "draw_line",
        Transaction = "AcadMcp: #1 draw_line",
        ObjectsAfter = handles,
        Mutated = true,
        Atomic = true,
    };

    private static StepRecord Planned(string op = "draw_room") => new()
    {
        State = StepState.Planned,
        Op = op,
    };

    /// <summary>A journal with <paramref name="count"/> applied steps, handle H1..Hn.</summary>
    private static Journal Applied(int count)
    {
        var journal = new Journal();
        for (var i = 1; i <= count; i++)
        {
            journal.AddExecuted(Done($"H{i}"));
        }
        return journal;
    }

    // ---------------------------------------------------------------- indices

    [Fact]
    public void IndicesAreOneBasedAndMatchPosition()
    {
        var journal = Applied(3);

        Assert.Equal(3, journal.Count);
        Assert.Equal(1, journal.At(1)!.Index);
        Assert.Equal(3, journal.At(3)!.Index);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(4)]
    [InlineData(-1)]
    public void AtReturnsNullOutsideTheJournal(int index)
    {
        Assert.Null(Applied(3).At(index));
    }

    [Fact]
    public void LastAtomicDoneSkipsATrailingRecordWithNoUndoEntry()
    {
        var journal = Applied(2);
        journal.AddExecuted(new StepRecord
        {
            State = StepState.Done,
            Op = "snapshot",
            Transaction = "",
            Mutated = false,
            Atomic = false,
        });

        // The newest record owns no transaction, so it must not be the anchor.
        Assert.Equal(2, journal.LastAtomicDone()!.Index);
    }

    // ---------------------------------------------------------------- planning

    [Fact]
    public void SetPlanAppendsAfterAppliedSteps()
    {
        var journal = Applied(2);

        var added = journal.SetPlan("a plan", [Planned(), Planned()]);

        Assert.Equal(2, added.Count);
        Assert.Equal(3, added[0].Index);
        Assert.Equal(4, added[1].Index);
        Assert.Equal(StepState.Planned, journal.At(4)!.State);
        Assert.Equal("a plan", journal.Description);
    }

    [Fact]
    public void SetPlanReplacesTheTailRatherThanStacking()
    {
        var journal = Applied(1);
        journal.SetPlan("first", [Planned(), Planned()]);

        journal.SetPlan("second", [Planned()]);

        Assert.Equal(2, journal.Count);
        Assert.Equal("second", journal.Description);
    }

    [Fact]
    public void SetPlanNeverRewritesAppliedHistory()
    {
        var journal = Applied(2);

        journal.SetPlan("plan", [Planned()]);

        Assert.Equal(StepState.Done, journal.At(1)!.State);
        Assert.Equal(StepState.Done, journal.At(2)!.State);
        Assert.Equal("H2", journal.At(2)!.ObjectsAfter[0]);
    }

    [Fact]
    public void SetPlanClearsReviewAndFailureMarksOnTheNewSteps()
    {
        var journal = new Journal();

        var added = journal.SetPlan("p", [Planned() with { Accepted = true, Error = "boom" }]);

        Assert.False(added[0].Accepted);
        Assert.Equal("", added[0].Error);
    }

    [Fact]
    public void DropPlannedLeavesAppliedStepsAndReindexes()
    {
        var journal = Applied(2);
        journal.SetPlan("p", [Planned(), Planned()]);

        Assert.Equal(2, journal.DropPlanned());

        Assert.Equal(2, journal.Count);
        Assert.Equal(1, journal.At(1)!.Index);
        Assert.Equal(2, journal.At(2)!.Index);
    }

    [Fact]
    public void NextRunnablePrefersTheEarliestPlannedStep()
    {
        var journal = Applied(1);
        journal.SetPlan("p", [Planned("a"), Planned("b")]);

        Assert.Equal("a", journal.NextRunnable()!.Op);
    }

    [Fact]
    public void AFailedStepIsRunnableAgain()
    {
        // Its transaction aborted, so the drawing is as if it never ran: a retry
        // is the natural response, not a manual repair.
        var journal = new Journal();
        journal.AddExecuted(new StepRecord { State = StepState.Failed, Op = "draw_room" });

        Assert.Equal(1, journal.NextRunnable()!.Index);
    }

    [Fact]
    public void NextRunnableIsNullWhenNothingIsPending()
    {
        Assert.Null(Applied(2).NextRunnable());
    }

    [Fact]
    public void PendingIndicesListsPlannedAndFailedSteps()
    {
        var journal = Applied(2);
        journal.SetPlan("p", [Planned(), Planned()]);

        Assert.Equal([3, 4], journal.PendingIndices());
    }

    // ---------------------------------------------------------------- rollback

    [Fact]
    public void RollbackToZeroAffectsEveryAppliedStep()
    {
        var plan = Applied(3).PlanRollback(0);

        Assert.True(plan.Allowed);
        Assert.Equal([1, 2, 3], plan.Affected);
        Assert.Equal(["H1", "H2", "H3"], plan.HandlesToErase);
    }

    [Fact]
    public void RollbackKeepsStepsAtOrBelowTheTarget()
    {
        var plan = Applied(3).PlanRollback(1);

        Assert.Equal([2, 3], plan.Affected);
        Assert.Equal(["H2", "H3"], plan.HandlesToErase);
    }

    [Fact]
    public void RollbackToTheLastStepAffectsNothing()
    {
        var plan = Applied(3).PlanRollback(3);

        Assert.True(plan.Allowed);
        Assert.Empty(plan.Affected);
        Assert.Empty(plan.HandlesToErase);
    }

    [Fact]
    public void CreatedSinceNeverErasesWhatAnEarlierStepOwns()
    {
        var journal = new Journal();
        journal.AddExecuted(Done("shared", "H1"));
        journal.AddExecuted(Done("shared", "H2"));

        // Step 2 reports the same handle as step 1; erasing it would take out
        // entities a surviving step is responsible for.
        Assert.Equal(["H2"], journal.CreatedSince(1));
    }

    [Fact]
    public void CreatedSinceDeduplicatesAcrossSteps()
    {
        var journal = new Journal();
        journal.AddExecuted(Done("H1"));
        journal.AddExecuted(Done("H2", "H2"));

        Assert.Equal(["H2"], journal.CreatedSince(1));
    }

    [Fact]
    public void CreatedSinceIsEmptyWhenNothingCameAfter()
    {
        Assert.Empty(Applied(2).CreatedSince(5));
    }

    [Fact]
    public void RollbackIsRefusedAcrossAnAcceptedStep()
    {
        var journal = Applied(2);
        journal.SetAccepted(1, true);

        var plan = journal.PlanRollback(0);

        Assert.False(plan.Allowed);
        Assert.Equal([1], plan.Accepted);
        Assert.Contains("accepted", plan.Refusal);
        Assert.Empty(plan.HandlesToErase);
    }

    [Fact]
    public void ForceRollsBackAcrossAnAcceptedStep()
    {
        var journal = Applied(2);
        journal.SetAccepted(1, true);

        var plan = journal.PlanRollback(0, force: true);

        Assert.True(plan.Allowed);
        Assert.Equal([1], plan.Accepted);
    }

    [Fact]
    public void RollbackIsRefusedAcrossAnUnattributableChange()
    {
        var journal = new Journal();
        journal.AddExecuted(Done("H1") with { Atomic = false, Mutated = true });

        var plan = journal.PlanRollback(0);

        Assert.False(plan.Allowed);
        Assert.Equal([1], plan.Blocking);
        Assert.Empty(plan.HandlesToErase);
    }

    [Fact]
    public void ANonMutatingRecordDoesNotBlockRollback()
    {
        // It provably changed nothing, so it cannot make erasure hit the wrong thing.
        var journal = new Journal();
        journal.AddExecuted(Done("H1") with { Atomic = false, Mutated = false });

        Assert.True(journal.PlanRollback(0).Allowed);
    }

    [Fact]
    public void PartialIsReportedWhenAStepCannotBeRecreated()
    {
        var journal = new Journal();
        journal.AddExecuted(Done("H1") with { Executable = false, Mutated = true });

        var plan = journal.PlanRollback(0);

        Assert.True(plan.Allowed);
        Assert.True(plan.Partial);
        Assert.Equal([1], plan.Unrecoverable);
        Assert.Contains("partial", plan.Describe());
    }

    [Theory]
    [InlineData(9)]
    [InlineData(-1)]
    public void RollbackToANonExistentStepIsRefused(int index)
    {
        var plan = Applied(2).PlanRollback(index);

        Assert.False(plan.Allowed);
        Assert.Contains("does not exist", plan.Refusal);
    }

    [Fact]
    public void StepsWithoutUndoListsAppliedMutatingRecordsWithNoTransaction()
    {
        var journal = new Journal();
        journal.AddExecuted(Done("H1"));
        journal.AddExecuted(Done("H2") with { Transaction = "" });

        Assert.Equal([2], journal.StepsWithoutUndo());
    }

    // ---------------------------------------------------------------- rewind

    [Fact]
    public void RewindDemotesAppliedStepsAboveTheTarget()
    {
        var journal = Applied(3);

        var touched = journal.Rewind(1);

        Assert.Equal([2, 3], touched);
        Assert.Equal(StepState.Done, journal.At(1)!.State);
        Assert.Equal(StepState.Planned, journal.At(2)!.State);
        Assert.Equal(StepState.Planned, journal.At(3)!.State);
    }

    [Fact]
    public void RewindClearsHandlesAndReviewSoARerunStartsClean()
    {
        var journal = Applied(2);
        journal.SetAccepted(2, true);

        journal.Rewind(1);

        var record = journal.At(2)!;
        Assert.Empty(record.ObjectsAfter);
        Assert.Empty(record.ObjectsBefore);
        Assert.False(record.Accepted);
        Assert.Equal("", record.Transaction);
    }

    [Fact]
    public void RewindAlsoResetsFailedStepsAboveTheTarget()
    {
        var journal = new Journal();
        journal.AddExecuted(Done("H1"));
        journal.AddExecuted(new StepRecord { State = StepState.Failed, Op = "draw_room", Error = "boom" });

        journal.Rewind(1);

        Assert.Equal(StepState.Planned, journal.At(2)!.State);
        Assert.Equal("", journal.At(2)!.Error);
    }

    [Fact]
    public void RewindToZeroResetsEverything()
    {
        var journal = Applied(2);

        Assert.Equal([1, 2], journal.Rewind(0));
        Assert.Empty(journal.Applied);
    }

    // ---------------------------------------------------------------- reject

    [Fact]
    public void RejectDropsRecordsAndReportsTheirHandles()
    {
        var journal = Applied(3);

        var (handles, removed) = journal.PlanReject(2)!.Value;

        Assert.Equal(["H2", "H3"], handles);
        Assert.Equal([2, 3], removed);
        Assert.Equal(1, journal.Count);
    }

    [Fact]
    public void RejectIgnoresTheAcceptedSoftLock()
    {
        // Reject is the deliberate destructive act; the records are going away
        // regardless, so a review mark must not stop it.
        var journal = Applied(2);
        journal.SetAccepted(2, true);

        var (handles, _) = journal.PlanReject(2)!.Value;

        Assert.Equal(["H2"], handles);
    }

    [Fact]
    public void RejectFromTheFirstStepEmptiesTheJournal()
    {
        var (handles, removed) = Applied(2).PlanReject(1)!.Value;

        Assert.Equal(["H1", "H2"], handles);
        Assert.Equal([1, 2], removed);
    }

    [Fact]
    public void RejectDoesNotEraseAHandleASurvivingStepAlsoCreated()
    {
        var journal = new Journal();
        journal.AddExecuted(Done("shared"));
        journal.AddExecuted(Done("shared"));

        var (handles, _) = journal.PlanReject(2)!.Value;

        Assert.Empty(handles);
    }

    // ---------------------------------------------------------------- edits

    [Fact]
    public void UpdateShallowMergesParams()
    {
        var journal = Applied(1);
        journal.Update(1, Patch(("thickness", 200)));

        journal.Update(1, Patch(("thickness", 240), ("hatch", true)));

        var parameters = journal.At(1)!.Params!;
        Assert.Equal(240, parameters["thickness"]!.GetValue<int>());
        Assert.True(parameters["hatch"]!.GetValue<bool>());
    }

    [Fact]
    public void UpdateOfLabelAloneRenamesTheStep()
    {
        var journal = Applied(1);

        journal.Update(1, Patch(("label", "south wall")));

        Assert.Equal("south wall", journal.At(1)!.Label);
        // Renaming is not an argument change, so it must not fabricate params.
        Assert.Null(journal.At(1)!.Params);
    }

    [Fact]
    public void UpdateOfAnUnknownStepReturnsNull()
    {
        Assert.Null(Applied(1).Update(7, Patch(("label", "x"))));
    }

    [Fact]
    public void MarkDriftedIsIdempotentAndReversible()
    {
        var journal = Applied(1);

        journal.MarkDrifted(1);
        Assert.True(journal.At(1)!.Drifted);

        journal.MarkDrifted(1, false);
        Assert.False(journal.At(1)!.Drifted);
    }

    [Fact]
    public void OwnerOfFindsTheMostRecentStepHoldingAHandle()
    {
        var journal = new Journal();
        journal.AddExecuted(Done("H1"));
        journal.AddExecuted(Done("H2"));

        Assert.Equal(1, journal.OwnerOf("H1"));
        Assert.Equal(2, journal.OwnerOf("H2"));
        Assert.Null(journal.OwnerOf("nope"));
    }

    [Fact]
    public void OwnerOfIgnoresHandleCase()
    {
        var journal = new Journal();
        journal.AddExecuted(Done("2F1"));

        Assert.Equal(1, journal.OwnerOf("2f1"));
    }

    [Fact]
    public void TrackedHandlesUnionsEveryStep()
    {
        var journal = new Journal();
        journal.AddExecuted(Done("H1"));
        journal.AddExecuted(Done("H2"));

        Assert.Equal(2, journal.TrackedHandles().Count);
    }

    // ---------------------------------------------------------------- replace

    [Fact]
    public void ReplaceSwapsTheWholeRecord()
    {
        var journal = Applied(2);

        journal.Replace(2, journal.At(2)! with
        {
            State = StepState.Failed,
            Error = "boom",
            ObjectsAfter = [],
            Transaction = "",
        });

        var record = journal.At(2)!;
        Assert.Equal(StepState.Failed, record.State);
        Assert.Equal("boom", record.Error);
        Assert.Empty(record.ObjectsAfter);
    }

    [Fact]
    public void ReplaceKeepsTheIndexCanonical()
    {
        var journal = Applied(2);

        // A record claiming the wrong index must be filed under the one asked for,
        // otherwise a step could end up addressing a different step than it is.
        journal.Replace(2, journal.At(2)! with { Index = 99 });

        Assert.Equal(2, journal.At(2)!.Index);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(3)]
    [InlineData(-1)]
    public void ReplaceOutsideTheJournalDoesNothing(int index)
    {
        var journal = Applied(2);
        var before = journal.At(1)!;

        Assert.Null(journal.Replace(index, new StepRecord { Op = "x" }));
        Assert.Equal(before.Op, journal.At(1)!.Op);
    }

    [Fact]
    public void ReplacingARecordWithNoIndexUsesThePosition()
    {
        var journal = Applied(1);

        journal.Replace(1, new StepRecord { State = StepState.Planned, Op = "draw_wall" });

        Assert.Equal(1, journal.At(1)!.Index);
        Assert.Equal(StepState.Planned, journal.At(1)!.State);
        Assert.True(journal.NextRunnable() is not null);
    }

    // ---------------------------------------------------------------- reporting

    [Fact]
    public void SummaryCountsEveryState()
    {
        var journal = Applied(2);
        journal.SetPlan("p", [Planned()]);
        journal.AddExecuted(new StepRecord { State = StepState.Failed, Op = "x" });
        journal.SetAccepted(1, true);

        var summary = journal.Summary();

        Assert.Contains("2 done", summary);
        Assert.Contains("1 planned", summary);
        Assert.Contains("1 failed", summary);
        Assert.Contains("1 accepted", summary);
    }

    [Fact]
    public void SummaryOfAnEmptyJournalIsJustZeroDone()
    {
        Assert.Equal("0 done", new Journal().Summary());
    }

    [Theory]
    [InlineData(StepState.Done, false, "+")]
    [InlineData(StepState.Done, true, "*")]
    [InlineData(StepState.Failed, false, "x")]
    [InlineData(StepState.Planned, false, ".")]
    public void StateMarkIsReadable(StepState state, bool accepted, string expected)
    {
        Assert.Equal(expected, new StepRecord { State = state, Accepted = accepted }.StateMark());
    }

    [Fact]
    public void DescribeUsesTheCallersOwnNameForWhatItDraws()
    {
        Assert.Equal("draw_room aseptic", Journal.Describe("draw_room", Patch(("name", "aseptic"))));
        Assert.Equal("draw_room", Journal.Describe("draw_room", Patch(("thickness", 200))));
        Assert.Equal("draw_room", Journal.Describe("draw_room", null));
    }

    [Fact]
    public void PlannedBuildsARunnableRecord()
    {
        var record = Journal.Planned("draw_room", Patch(("name", "lab")));

        Assert.Equal(StepState.Planned, record.State);
        Assert.Equal("draw_room lab", record.Label);
        Assert.True(record.IsRunnable);
    }

    // ---------------------------------------------------------------- the fixes

    [Theory]
    [InlineData(0)]
    [InlineData(-1)]
    [InlineData(4)]
    public void RejectingANonExistentStepIsRefusedRatherThanThrowing(int index)
    {
        // RemoveRange raised ArgumentOutOfRangeException for every one of these, so the caller
        // saw "Non-negative number required" instead of a refusal. Index 0 is the value that
        // made it likely: `rollback_to` accepts and documents it as "before every step", and
        // reject takes the same argument. Unlike a rollback there is no safe reading of 0 for
        // an erase that cannot be taken back, so it is refused by name.
        var journal = Applied(3);

        Assert.Null(journal.PlanReject(index));
        Assert.Equal(3, journal.Count);
    }

    [Fact]
    public void RejectingTheLastStepIsAllowed()
    {
        var journal = Applied(3);

        var plan = journal.PlanReject(3);

        Assert.NotNull(plan);
        Assert.Equal([3], plan.Value.Removed);
        Assert.Equal(2, journal.Count);
    }

    [Fact]
    public void ASnapshotIsNotReportedAsSomethingARollbackCannotRecreate()
    {
        // The baseline is stored Executable = false, Mutated = false precisely because it owns
        // no geometry — and `Mutated && Executable` read that as "cannot be re-created", so
        // every rollback past a baseline warned about a step that never drew anything.
        var journal = Applied(1);
        journal.AddExecuted(new StepRecord
        {
            State = StepState.Done,
            Op = "snapshot",
            Mutated = false,
            Executable = false,
        });
        journal.AddExecuted(Done("H2"));

        var plan = journal.PlanRollback(0);

        Assert.True(plan.Allowed);
        Assert.False(plan.Partial);
        Assert.Empty(plan.Unrecoverable);
        Assert.DoesNotContain("partial", plan.Describe());
    }

    [Fact]
    public void UpdateLeavesTheCallersPatchAlone()
    {
        var journal = Applied(1);
        var patch = Patch(("label", "renamed"), ("note", "checked"), ("width", 900));

        journal.Update(1, patch);

        // Applying a patch used to empty it, which a caller that built one patch and reused
        // it would find out about only on the second step.
        Assert.Equal(["label", "note", "width"], patch.Select(pair => pair.Key));
        Assert.Equal("renamed", journal.At(1)!.Label);
        Assert.Equal("checked", journal.At(1)!.Note);
        Assert.Equal(900, journal.At(1)!.Params!["width"]!.GetValue<int>());
    }

    private static System.Text.Json.Nodes.JsonObject Patch(
        params (string Key, object? Value)[] pairs)
    {
        var obj = new System.Text.Json.Nodes.JsonObject();
        foreach (var (key, value) in pairs)
        {
            obj[key] = value is null
                ? null
                : System.Text.Json.Nodes.JsonValue.Create(value);
        }
        return obj;
    }
}
