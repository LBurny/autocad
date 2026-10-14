using System.Diagnostics;
using System.Text;
using AcadMcp.Core;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Runtime;
using AcadApp = Autodesk.AutoCAD.ApplicationServices.Application;

namespace AcadMcp.Plugin;

/// <summary>
/// An in-AutoCAD self-test of the step engine, run with <c>ACADMCPTEST</c>.
/// </summary>
/// <remarks>
/// <para>
/// This exists because a .NET plugin cannot be unloaded from AutoCAD: every code
/// change costs a restart to try, so the fastest possible feedback loop is worth
/// building. The test drives the engine through a full apply/rollback/rerun cycle
/// and reports each check, rather than leaving a person to eyeball a drawing and
/// guess whether a rollback really removed the right entities.
/// </para>
/// <para>
/// It runs inside a scratch drawing it creates and discards, so it never touches
/// whatever the user has open.
/// </para>
/// </remarks>
public static class SelfTest
{
    [CommandMethod("ACADMCPTEST")]
    public static void RunFromCommandLine()
    {
        var report = Run();
        foreach (var line in report.Split('\n'))
        {
            AcadApp.DocumentManager.MdiActiveDocument?.Editor.WriteMessage("\n" + line);
        }
    }

    /// <summary>Run the apply/rollback checks and return a report. Never throws.</summary>
    public static string Run(bool writeLog = true)
    {
        var report = new Report();
        Document? scratch = null;
        var restoreTo = AcadApp.DocumentManager.MdiActiveDocument;
        try
        {
            // A scratch drawing: the engine writes Xrecords and entities, and the
            // user's open drawing must not be the thing under test.
            scratch = AcadApp.DocumentManager.Add("");
            var engine = StepEngine.For(scratch);
            RunCoreChecks(engine, scratch.Database, report);
        }
        catch (System.Exception error)
        {
            report.Crashed(error);
            PluginLog.Write("self-test threw", error);
        }
        finally
        {
            Discard(scratch, restoreTo, report);
        }

        var text = report.Finish();
        if (writeLog)
        {
            PluginLog.Write("self-test report\n" + text);
        }
        return text.TrimEnd();
    }

    /// <summary>
    /// Run every check <em>plus</em> the undo-interaction probe, which cannot
    /// finish before returning.
    /// </summary>
    /// <remarks>
    /// <para>
    /// The probe answers the one question the rollback design rests on: after the
    /// plugin rolls back, does a single Ctrl+Z put the drawing and the journal
    /// back together? The claim being tested is that the erases and the journal
    /// write share one transaction, so AutoCAD records them as one undo step. If
    /// that is false, the journal would credit a step with entities that are no
    /// longer there, and the next rollback would erase the wrong things.
    /// </para>
    /// <para>
    /// It has to be split in two because the undo has to be performed the way the
    /// user performs it — from the command line with no command active. Calling
    /// <c>Editor.Command</c> from here throws <c>eInvalidInput</c>, since this runs
    /// from an idle handler rather than inside a command; queueing the command
    /// string defers it until AutoCAD is idle, which is exactly the situation
    /// Ctrl+Z happens in. The second phase therefore runs from a later idle tick
    /// and writes the report itself.
    /// </para>
    /// </remarks>
    /// <returns>
    /// True when <see cref="FinishProbe"/> will report the run later, false when
    /// it has already reported.
    /// </returns>
    public static bool RunUndoProbe()
    {
        var report = new Report();
        Document? scratch = null;
        var restoreTo = AcadApp.DocumentManager.MdiActiveDocument;
        try
        {
            scratch = AcadApp.DocumentManager.Add("");
            var engine = StepEngine.For(scratch);
            RunCoreChecks(engine, scratch.Database, report);

            // The core checks end with the journal reset, so this needs its own
            // three steps. Six entities in total.
            var (wall, room, stair) = Seed(engine);
            report.Check("re-seeded for the undo check", wall.Ok && room.Ok && stair.Ok, stair.Message);

            // Read the handles before the rollback. A rollback clears the handle
            // list of every step it reverts — that is what makes the re-run able
            // to recreate them — so collecting them afterwards would leave only
            // the surviving step and make every later check pass vacuously.
            var handles = engine.Journal.Records.SelectMany(r => r.ObjectsAfter).ToList();
            var seeded = CountPresent(scratch.Database, handles);
            report.Check("the scenario drew every entity", seeded == 6,
                $"present={seeded} of {handles.Count}");

            var rollback = engine.RollbackTo(1);
            report.Check("rollback before the undo check succeeds", rollback.Ok, rollback.Message);

            var before = CountPresent(scratch.Database, handles);
            report.Check("the rollback removed the steps above the target",
                before == 1, $"present={before} of {handles.Count}");

            _probe = new ProbeState
            {
                Report = report,
                Document = scratch,
                RestoreTo = restoreTo,
                Handles = handles,
                PresentBefore = before,
            };

            // activate: true matters — the command has to run in the scratch
            // drawing, not in whatever the user has in front of them.
            scratch.SendStringToExecute("_.UNDO 1 ", true, false, false);
            _probe.Clock.Restart();
            _probe.Handler = (_, _) => ProbeOnIdle();
            AcadApp.Idle += _probe.Handler;
            return true;
        }
        catch (System.Exception error)
        {
            report.Crashed(error);
            PluginLog.Write("the undo probe threw", error);
            Discard(scratch, restoreTo, report);
            PluginLog.Write("self-test report\n" + report.Finish());
            PluginLog.Write("AUTO-SELFTEST-DONE");
            return false;
        }
    }

    /// <summary>Second phase: the queued undo has run, so check what it did.</summary>
    private static void ProbeOnIdle()
    {
        var probe = _probe;
        if (probe is null)
        {
            return;
        }

        var present = CountPresent(probe.Document.Database, probe.Handles);
        var changed = present != probe.PresentBefore;
        if (changed)
        {
            // The undo landed. A few more idle ticks cost nothing and avoid
            // reading the database while AutoCAD is still finishing it.
            if (probe.Clock.ElapsedMilliseconds < UndoQuietMillis)
            {
                return;
            }
        }
        else if (probe.Clock.Elapsed.TotalSeconds < UndoGiveUpSeconds)
        {
            return;
        }

        FinishProbe(present);
    }

    /// <summary>
    /// Report on the undo, discard the scratch drawing, and write the marker the
    /// harness waits for.
    /// </summary>
    private static void FinishProbe(int presentAfterUndo)
    {
        var probe = _probe;
        if (probe is null)
        {
            return;
        }
        _probe = null;
        if (probe.Handler is not null)
        {
            AcadApp.Idle -= probe.Handler;
        }

        try
        {
            var report = probe.Report;
            var total = probe.Handles.Count;
            var detail = presentAfterUndo == probe.PresentBefore
                ? $"the undo changed nothing: present={presentAfterUndo} of {total}"
                : $"present={presentAfterUndo} of {total}";
            report.Check("one Ctrl+Z restored every entity the rollback erased",
                presentAfterUndo == total, detail);

            var journal = StepEngine.For(probe.Document).Reload();
            var applied = journal.Applied.Count();
            var recorded = journal.Records.SelectMany(r => r.ObjectsAfter).Count();
            report.Check("the undo also restored the journal's handle lists",
                applied == 3 && recorded == total,
                $"applied={applied} recorded={recorded} handles of {total}");

            // The invariant that actually matters: an applied step must own its
            // entities and be able to name them, a reverted one must own none.
            // "An applied step with no handles recorded" counts as a mismatch on
            // purpose — without it an empty handle list would satisfy the check
            // trivially and the whole test would prove nothing.
            var mismatches = new List<string>();
            foreach (var record in journal.Records)
            {
                var owned = record.ObjectsAfter.Count;
                var present = CountPresent(probe.Document.Database, record.ObjectsAfter);
                if (record.IsApplied && owned == 0)
                {
                    mismatches.Add($"step {record.Index} is applied but records no handles");
                }
                else if (record.IsApplied && present != owned)
                {
                    mismatches.Add($"step {record.Index} is applied but only {present}/{owned} entities remain");
                }
                else if (!record.IsApplied && owned != 0)
                {
                    mismatches.Add($"step {record.Index} is not applied but still records {owned} handles");
                }
            }
            report.Check("the journal still agrees with the drawing after the undo",
                mismatches.Count == 0,
                mismatches.Count == 0
                    ? $"applied={applied} entities={presentAfterUndo}"
                    : string.Join("; ", mismatches));

            Discard(probe.Document, probe.RestoreTo, report);
            PluginLog.Write("self-test report\n" + report.Finish());
        }
        catch (System.Exception error)
        {
            probe.Report.Crashed(error);
            PluginLog.Write("the undo probe failed to report", error);
            PluginLog.Write("self-test report\n" + probe.Report.Finish());
        }
        finally
        {
            PluginLog.Write("AUTO-SELFTEST-DONE");
        }
    }

    /// <summary>
    /// The apply / round-trip / rollback / rerun / refusal checks. They write
    /// into <paramref name="report"/> so a caller can own the report and, in the
    /// probe's case, keep adding to it after this returns.
    /// </summary>
    private static void RunCoreChecks(StepEngine engine, Database db, Report report)
    {
        report.Check("fresh drawing has an empty journal", engine.Journal.IsEmpty);

        // --- apply three steps -------------------------------------------
        var (wall, room, stair) = Seed(engine);
        report.Check("apply step 1 succeeds", wall.Ok, wall.Message);
        report.Check("apply step 2 (polyline, CJK text, hatch) succeeds", room.Ok, room.Message);
        report.Check("apply step 3 (circle, arc) succeeds", stair.Ok, stair.Message);

        report.Check("journal holds three applied steps", engine.Journal.Count == 3,
            $"count={engine.Journal.Count}");

        var handles = engine.Journal.Records.SelectMany(r => r.ObjectsAfter).ToList();
        report.Check("every step recorded its handles", handles.Count == 6, $"handles={handles.Count}");
        report.Check("recorded handles exist in the drawing", CountPresent(db, handles) == 6,
            $"present={CountPresent(db, handles)}");

        // --- the journal survives a re-read from the drawing --------------
        var reloaded = engine.Reload();
        report.Check("journal round-trips through the DWG", reloaded.Count == 3, $"count={reloaded.Count}");
        report.Check("CJK text survives the DWG round trip",
            reloaded.At(2)!.Primitives.Any(p => p.Text == "\u65e0\u83cc\u5ba4"));
        report.Check("params and labels survive", reloaded.At(2)!.Label == "room",
            $"label={reloaded.At(2)!.Label}");

        // --- rollback -----------------------------------------------------
        var rollback = engine.RollbackTo(1);
        report.Check("rollback to step 1 succeeds", rollback.Ok, rollback.Message);
        report.Check("journal keeps only step 1 applied", engine.Journal.Applied.Count() == 1,
            $"applied={engine.Journal.Applied.Count()}");
        report.Check("steps 2 and 3 are planned again",
            engine.Journal.At(2)!.State == StepState.Planned
            && engine.Journal.At(3)!.State == StepState.Planned);

        var survivors = engine.Journal.At(1)!.ObjectsAfter.ToList();
        report.Check("step 1's entities survived", CountPresent(db, survivors) == 1,
            $"present={CountPresent(db, survivors)}");

        var doomed = handles.Where(h => !survivors.Contains(h)).ToList();
        report.Check("every handle above the target was erased", CountPresent(db, doomed) == 0,
            $"still present={CountPresent(db, doomed)}");

        // --- rerun --------------------------------------------------------
        var rerun = engine.RunAll();
        report.Check("run_all after rollback succeeds", rerun.Ok, rerun.Message);
        report.Check("all three steps applied again", engine.Journal.Applied.Count() == 3,
            $"applied={engine.Journal.Applied.Count()}");
        var afterRerun = engine.Journal.Records.SelectMany(r => r.ObjectsAfter).ToList();
        report.Check("re-run recreated every entity", CountPresent(db, afterRerun) == 6,
            $"present={CountPresent(db, afterRerun)}");

        // --- rollback everything ------------------------------------------
        var wipe = engine.RollbackTo(0);
        report.Check("rollback to 0 succeeds", wipe.Ok, wipe.Message);
        report.Check("nothing is left applied", !engine.Journal.Applied.Any());
        report.Check("the drawing has no journal entities left",
            CountPresent(db, afterRerun) == 0, $"present={CountPresent(db, afterRerun)}");

        // --- refusals ------------------------------------------------------
        engine.RunAll();
        engine.SetAccepted(1, true);
        var refused = engine.RollbackTo(0);
        report.Check("rollback across an accepted step is refused", !refused.Ok, refused.Message);
        var forced = engine.RollbackTo(0, force: true);
        report.Check("force overrides the refusal", forced.Ok, forced.Message);

        engine.Reset();
        report.Check("reset empties the journal", engine.Journal.IsEmpty);
    }

    /// <summary>
    /// The three demo steps every scenario draws: a wall line, a room of
    /// polyline + CJK text + hatch, and a stair of a circle and an arc — six
    /// entities in all.
    /// </summary>
    private static (StepOutcome Wall, StepOutcome Room, StepOutcome Stair) Seed(StepEngine engine)
    {
        var wall = engine.Apply("draw_wall", "wall", null,
        [
            new Primitive
            {
                Kind = PrimitiveKind.Line,
                Points = [0, 0, 6000, 0],
                Layer = "A-WALL",
            },
        ]);

        var room = engine.Apply("draw_room", "room", null,
        [
            new Primitive
            {
                Kind = PrimitiveKind.Polyline,
                Points = [0, 0, 4000, 0, 4000, 3000, 0, 3000],
                Closed = true,
                Layer = "A-WALL",
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
            new Primitive
            {
                Kind = PrimitiveKind.Hatch,
                Points = [0, 0, 4000, 0, 4000, 3000, 0, 3000],
                Pattern = "ANSI31",
                Scale = 40,
                Layer = "A-HATCH",
            },
        ]);

        var stair = engine.Apply("draw_stair", "stair", null,
        [
            new Primitive { Kind = PrimitiveKind.Circle, Points = [7000, 2000], Radius = 400, Layer = "A-STAIR" },
            new Primitive
            {
                Kind = PrimitiveKind.Arc,
                Points = [7000, 2000],
                Radius = 400,
                StartAngle = 0,
                EndAngle = 90,
                Layer = "A-STAIR",
            },
        ]);

        return (wall, room, stair);
    }

    private static void Discard(Document? scratch, Document? restoreTo, Report report)
    {
        try
        {
            if (restoreTo is not null && !restoreTo.IsDisposed)
            {
                AcadApp.DocumentManager.MdiActiveDocument = restoreTo;
            }
            scratch?.CloseAndDiscard();
        }
        catch (System.Exception error)
        {
            report.Check("the scratch drawing can be discarded", false, error.Message);
        }
    }

    /// <summary>How many of these handles still resolve to a live object.</summary>
    private static int CountPresent(Database db, IEnumerable<string> handles)
    {
        var present = 0;
        using var tr = db.TransactionManager.StartTransaction();
        foreach (var text in handles)
        {
            if (!long.TryParse(text, System.Globalization.NumberStyles.HexNumber,
                    System.Globalization.CultureInfo.InvariantCulture, out var value))
            {
                continue;
            }
            if (db.TryGetObjectId(new Handle(value), out var id)
                && !id.IsNull && id.IsValid && !id.IsErased)
            {
                present++;
            }
        }
        tr.Commit();
        return present;
    }

    /// <summary>How long to keep watching after the undo is seen to have landed.</summary>
    private const int UndoQuietMillis = 1500;

    /// <summary>How long to wait for the undo to have any effect before reporting.</summary>
    private const double UndoGiveUpSeconds = 20;

    private static ProbeState? _probe;

    /// <summary>State the second phase of the undo probe needs after returning.</summary>
    private sealed class ProbeState
    {
        public required Report Report { get; init; }
        public required Document Document { get; init; }
        public required Document? RestoreTo { get; init; }
        public required IReadOnlyList<string> Handles { get; init; }
        public required int PresentBefore { get; init; }
        public Stopwatch Clock { get; } = new();
        public EventHandler? Handler { get; set; }
    }

    /// <summary>
    /// Collects checks for one run. Holding the failure count here, rather than
    /// in a local, is what lets the undo probe's second phase keep scoring
    /// against the same report after the first phase has returned.
    /// </summary>
    private sealed class Report
    {
        private readonly StringBuilder _text = new();

        public int Failures { get; private set; }

        public void Check(string name, bool passed, string detail = "")
        {
            if (!passed)
            {
                Failures++;
            }
            // ASCII punctuation only: the report is also read by scripts, and a
            // non-ASCII dash turns into mojibake in whatever encoding the reader
            // happens to assume.
            _text.Append($"[{(passed ? "pass" : "FAIL")}] {name}{(detail.Length > 0 ? " -- " + detail : "")}\n");
        }

        public void Crashed(System.Exception error)
        {
            Check("the self-test completed", false, $"{error.GetType().Name}: {error.Message}");
            _text.Append(error.StackTrace).Append('\n');
        }

        /// <summary>The report with the summary line the harness reads appended.</summary>
        public string Finish()
        {
            _text.Append(Failures == 0
                ? "\nACADMCPTEST: all checks passed\n"
                : $"\nACADMCPTEST: {Failures} CHECK(S) FAILED\n");
            return _text.ToString();
        }
    }
}
