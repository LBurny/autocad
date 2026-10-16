using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using AcadMcp.Core;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
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
        DiscardLibrary();
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

            // Plus a fourth step that inserts a block, because the undo has to cope with
            // a step whose definition lives on past it: the rollback below must not take
            // the definition down, and the undo must bring the reference back resolving
            // to it.
            var library = Library(report);
            var column = library is null
                ? StepOutcome.Fail("place_column", "no test library")
                : engine.Apply("place_column", "column", null,
                [
                    new Primitive
                    {
                        Kind = PrimitiveKind.Insert,
                        Points = [3000, 2500],
                        Block = LibraryBlock,
                        Source = library,
                        Layer = "S-COLUMN",
                    },
                ]);
            var inserted = column.Ok ? engine.Journal.At(4)?.ObjectsAfter.FirstOrDefault() ?? "" : "";

            // Read the handles before the rollback. A rollback clears the handle
            // list of every step it reverts — that is what makes the re-run able
            // to recreate them — so collecting them afterwards would leave only
            // the surviving step and make every later check pass vacuously.
            var handles = engine.Journal.Records.SelectMany(r => r.ObjectsAfter).ToList();
            var seeded = CountPresent(scratch.Database, handles);
            report.Check("the scenario drew every entity", seeded == 7,
                $"present={seeded} of {handles.Count}");
            report.Check("it includes a block reference", inserted.Length > 0
                && BlockOf(scratch.Database, inserted) == LibraryBlock,
                $"handle={inserted}, block={BlockOf(scratch.Database, inserted) ?? "(no reference)"}");

            // The panel is otherwise only ever verified by a person looking at it, so
            // here it is built and shown. Showing a palette without being asked is
            // normally hostile — this only ever happens in test mode, and the
            // palette's own constructor throwing would take AutoCAD's UI down.
            try
            {
                using (var control = new StepsControl())
                {
                    report.Check("the steps palette control can be built",
                        control.Controls.Count > 0, $"{control.Controls.Count} child control(s)");
                }
                StepPanel.Show();
                report.Check("the steps palette opens", StepPanel.IsVisible);
            }
            catch (System.Exception error)
            {
                report.Check("the steps palette can be shown", false, error.Message);
            }

            var rollback = engine.RollbackTo(1);
            report.Check("rollback before the undo check succeeds", rollback.Ok, rollback.Message);

            var before = CountPresent(scratch.Database, handles);
            report.Check("the rollback removed the steps above the target",
                before == 1, $"present={before} of {handles.Count}");
            report.Check("the rollback left the inserted block's definition behind",
                library is null || HasBlock(scratch.Database, LibraryBlock));

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
                applied == journal.Records.Count() && recorded == total,
                $"applied={applied} of {journal.Records.Count} recorded={recorded} handles of {total}");

            // The reference the undo brought back has to point at a definition that is
            // still there. If the undo restored the reference and not the definition —
            // or the other way round — the drawing would hold a block reference to
            // nothing, which is the state this whole design is arranged to avoid.
            var restored = journal.Records
                .SelectMany(r => r.ObjectsAfter)
                .Select(handle => BlockOf(probe.Document.Database, handle))
                .Where(name => name is not null)
                .ToList();
            report.Check("the reference the undo restored still resolves to a real definition",
                restored.Count == 1 && restored[0] == LibraryBlock
                && HasBlock(probe.Document.Database, LibraryBlock),
                $"references={restored.Count}, definition present={HasBlock(probe.Document.Database, LibraryBlock)}");

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

            // Frame what was drawn, so a photograph of the palette has a plan behind
            // it rather than empty space. Queued for the same reason the undo was: a
            // command cannot be run from here.
            probe.Document.SendStringToExecute("_.ZOOM E ", true, false, false);

            DiscardLibrary();

            // The drawing is deliberately left open *and active*. This is the
            // unattended test mode, in a scratch AutoCAD the harness closes, and the
            // harness photographs the palette afterwards — which needs the active
            // drawing to be the one the journal has steps in. AutoCAD creates its own
            // empty Drawing1 at startup, so restoring the previous active document
            // here would leave the palette showing that one instead.
            var active = AcadApp.DocumentManager.MdiActiveDocument;
            PluginLog.Write(
                $"self-test finished: drew in '{probe.Document.Name}', active is "
                + $"'{active?.Name ?? "(none)"}' with {StepEngine.For(active ?? probe.Document).Journal.Count} step(s)");

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
        CheckRoundTrip(engine, report, "three applied steps");

        // --- Chinese must not come out as question marks -------------------
        // Storing the right characters is not enough: an SHX font with no big font
        // renders every one of them as '?', so the style the text was drawn with is
        // part of whether this worked.
        var roomText = DescribeText(db, "\u65e0\u83cc\u5ba4");
        report.Check("the room text is in the drawing", roomText is not null);
        if (roomText is not null)
        {
            report.Check("it uses the CJK text style",
                string.Equals(roomText.Style, TextStyles.Name, StringComparison.OrdinalIgnoreCase),
                $"style={roomText.Style}");
            report.Check("that style carries the SHX big font the glyphs need",
                string.Equals(roomText.BigFont, "gbcbig.shx", StringComparison.OrdinalIgnoreCase)
                && string.Equals(roomText.Font, "gbenor.shx", StringComparison.OrdinalIgnoreCase),
                $"font={roomText.Font} big={roomText.BigFont}");
            report.Check("repointing the style did not clobber the text height",
                Math.Abs(roomText.Height - 350) < 1e-6, $"height={roomText.Height}");
        }

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
        CheckRoundTrip(engine, report, "a rollback");

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
        CheckRoundTrip(engine, report, "running a plan");

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
        CheckRoundTrip(engine, report, "a forced rollback");

        // The plan-editing calls save through their own path, not through a step's
        // transaction, so they get their own round trips.
        engine.Update(1, new JsonObject { ["note"] = "annotated by the test" });
        CheckRoundTrip(engine, report, "an update");
        engine.SetPlan("a test plan", [new StepRecord { Op = "planned_wall" }]);
        CheckRoundTrip(engine, report, "a plan");
        engine.ClearPlan();
        CheckRoundTrip(engine, report, "clear_plan");

        engine.Reset();
        report.Check("reset empties the journal", engine.Journal.IsEmpty);
        CheckRoundTrip(engine, report, "a reset");

        CheckBlocks(engine, db, report);

        CheckErasedHandles(engine, db, report);

        MeasureAtScale(engine, report);
    }

    /// <summary>
    /// A rollback across an entity a person erased by hand.
    /// </summary>
    /// <remarks>
    /// <para>
    /// This is the case the whole erase-by-handle design exists to tolerate — "it is not
    /// disturbed by the user having typed a few commands in between" — and it did not work.
    /// <see cref="StepEngine.Erase"/> guarded on <c>IsNull</c>/<c>IsValid</c> and then opened the
    /// handle with <c>openErased: false</c>, which <em>raises</em> <c>eWasErased</c> instead of
    /// answering null, so the <c>obj is null || obj.IsErased</c> test after it was unreachable
    /// and the erased handle was never skipped. Measured live on the build before the guard
    /// went in, with one entity of step 2 deleted through COM: <c>rollback_to(1)</c> answered
    /// <c>{"ok": false, "message": "rollback failed: eWasErased"}</c>, <c>touched</c> was empty,
    /// and the journal still said two steps were applied while one entity was gone. The same
    /// call with both entities alive answered <c>ok: true, "erased 1 entity/ies"</c>.
    /// </para>
    /// <para>
    /// Which also settles the question the guard rested on: <c>ObjectId.IsValid</c> is
    /// <b>true</b> for an erased object. If it had been false, the old guard would have
    /// skipped the handle and no <c>eWasErased</c> could have been raised.
    /// </para>
    /// </remarks>
    private static void CheckErasedHandles(StepEngine engine, Database db, Report report)
    {
        var (wall, room, stair) = Seed(engine);
        report.Check("a scenario is drawn for the erased-handle check",
            wall.Ok && room.Ok && stair.Ok, room.Message);

        var doomed = engine.Journal.At(2)!.ObjectsAfter.FirstOrDefault() ?? "";
        report.Check("the step under test owns an entity to delete", doomed.Length > 0, doomed);

        EraseByHand(engine.Document, doomed);
        report.Check("the entity is gone while the journal still claims the step",
            CountPresent(db, [doomed]) == 0 && engine.Journal.At(2)!.IsApplied,
            $"present={CountPresent(db, [doomed])} state={engine.Journal.At(2)!.State}");

        var reverted = engine.RollbackTo(1);
        report.Check("a rollback across an entity erased by hand succeeds",
            reverted.Ok, reverted.Message);
        report.Check("it reverted the step rather than aborting half way",
            engine.Journal.Applied.Count() == 1 && engine.Journal.At(2)!.State == StepState.Planned,
            $"applied={engine.Journal.Applied.Count()} state={engine.Journal.At(2)!.State}");

        // The step above the deleted entity still has to go, or the tolerant skip would have
        // become a silent no-op: one erased handle must not stop the rest of the rollback.
        var survivorAbove = engine.Journal.At(3)!.ObjectsAfter.ToList();
        report.Check("and its neighbours' entities went with it",
            CountPresent(db, survivorAbove) == 0, $"present={CountPresent(db, survivorAbove)}");

        engine.RollbackTo(0, force: true);
        engine.Reset();
        report.Check("the erased-handle check left no steps behind", engine.Journal.IsEmpty);
    }

    /// <summary>Delete one entity the way the user would: outside any step, by hand.</summary>
    /// <remarks>
    /// It takes the document, not the database, because opening anything <c>ForWrite</c> in the
    /// open drawing needs the document lock: without it AutoCAD answers <c>eLockViolation</c>.
    /// That is the same rule the engine's own readers and writers follow, and the first version
    /// of this helper learned it the hard way — the test suite stopped at this check.
    /// </remarks>
    private static void EraseByHand(Document document, string handle)
    {
        var db = document.Database;
        if (!long.TryParse(handle, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var value)
            || !db.TryGetObjectId(new Handle(value), out var id) || id.IsNull || id.IsErased)
        {
            return;
        }
        using var docLock = document.LockDocument();
        using var tr = db.TransactionManager.StartTransaction();
        if (tr.GetObject(id, OpenMode.ForWrite, openErased: false) is Entity entity && !entity.IsErased)
        {
            entity.Erase();
        }
        tr.Commit();
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
        RestoreActiveDocument(restoreTo);
        try
        {
            scratch?.CloseAndDiscard();
        }
        catch (System.Exception error)
        {
            report.Check("the scratch drawing can be discarded", false, error.Message);
        }
    }

    private static void RestoreActiveDocument(Document? document)
    {
        try
        {
            if (document is not null && !document.IsDisposed)
            {
                AcadApp.DocumentManager.MdiActiveDocument = document;
            }
        }
        catch (System.Exception error)
        {
            PluginLog.Write("could not restore the active document", error);
        }
    }

    /// <summary>
    /// Read the journal back out of the drawing and compare it with the in-memory one,
    /// field by field.
    /// </summary>
    /// <remarks>
    /// This is the guard on saving only what changed: a missed write leaves the drawing
    /// holding an older journal than the one in memory, which no other check here would
    /// notice — the in-memory copy drives every action, so everything would keep looking
    /// right until the drawing was saved and reopened. Comparing the two serialised
    /// payloads covers every persisted field at once, and does it after every kind of
    /// mutation rather than after a representative one.
    /// </remarks>
    private static void CheckRoundTrip(StepEngine engine, Report report, string what)
    {
        var memory = JournalJson.ToJson(engine.Journal);
        var onDisk = JournalJson.ToJson(engine.Reload());
        var same = memory == onDisk;
        report.Check($"the journal in the drawing matches memory after {what}", same,
            same ? $"{engine.Journal.Count} step(s)" : $"memory={memory.Length} chars, disk={onDisk.Length} chars");
    }

    // ---------------------------------------------------------------- blocks

    /// <summary>The block the self-test's own library drawing defines.</summary>
    private const string LibraryBlock = "TEST-COLUMN";

    private static string? _library;

    /// <summary>
    /// Write a one-block library drawing, once per run, and return its path.
    /// </summary>
    /// <remarks>
    /// A library of our own rather than one of AutoCAD's shipped sample drawings: this has to
    /// mean the same thing on a machine where the samples were never installed, and a check
    /// that quietly does nothing there would be worse than no check at all.
    /// </remarks>
    private static string? Library(Report report)
    {
        if (_library is not null)
        {
            return _library;
        }

        var path = Path.Combine(Path.GetTempPath(), "acadmcp-selftest-library.dwg");
        try
        {
            // buildDefaultDrawing: true, so this is a normal drawing rather than an empty
            // database that AutoCAD might refuse to read back.
            using var library = new Database(true, true);
            using (var tr = library.TransactionManager.StartTransaction())
            {
                var table = (BlockTable)tr.GetObject(library.BlockTableId, OpenMode.ForWrite);
                var definition = new BlockTableRecord { Name = LibraryBlock };
                table.Add(definition);
                tr.AddNewlyCreatedDBObject(definition, true);

                // A 400 x 400 section with its insertion point at the centre, so a wrong
                // insertion point or a wrong scale shows up in the measured extents.
                var section = new Polyline();
                section.AddVertexAt(0, new Point2d(-200, -200), 0, 0, 0);
                section.AddVertexAt(1, new Point2d(200, -200), 0, 0, 0);
                section.AddVertexAt(2, new Point2d(200, 200), 0, 0, 0);
                section.AddVertexAt(3, new Point2d(-200, 200), 0, 0, 0);
                section.Closed = true;
                definition.AppendEntity(section);
                tr.AddNewlyCreatedDBObject(section, true);
                tr.Commit();
            }

            library.SaveAs(path, DwgVersion.Current);
            _library = path;
            report.Note($"test library written to {path}");
            return path;
        }
        catch (System.Exception error)
        {
            report.Check("the test library drawing can be written", false, error.Message);
            return null;
        }
    }

    /// <summary>Delete the test library, so a run does not leave files in temp.</summary>
    private static void DiscardLibrary()
    {
        var path = _library;
        _library = null;
        if (path is null)
        {
            return;
        }
        try
        {
            File.Delete(path);
        }
        catch (System.Exception error)
        {
            PluginLog.Write($"could not delete the test library {path}", error);
        }
    }

    /// <summary>
    /// The block checks: an insert is a step, but the definition behind it is document state.
    /// </summary>
    /// <remarks>
    /// The rule being pinned is the one that makes an insert safe to roll back — a step owns the
    /// reference it creates and nothing else. So reverting that step, or every step, must leave
    /// the definition standing: take it down with the step that imported it and every step still
    /// applied would be left pointing at nothing. The counter-rule is that a second insert must
    /// find the definition already there rather than importing a duplicate of it, which would
    /// give two blocks that look identical and behave differently.
    /// </remarks>
    private static void CheckBlocks(StepEngine engine, Database db, Report report)
    {
        var library = Library(report);
        if (library is null)
        {
            return;
        }

        // --- the insert, and the import behind it ---------------------------
        var rotated = engine.Apply("place_column", "column", null,
        [
            new Primitive
            {
                Kind = PrimitiveKind.Insert,
                Points = [3000, 2500],
                Block = LibraryBlock,
                Source = library,
                Rotation = 30,
                ScaleX = 1.5,
                ScaleY = 2.0,
                Layer = "S-COLUMN",
            },
        ]);
        report.Check("a step that inserts a block succeeds", rotated.Ok, rotated.Message);
        report.Check("importing the definition put it into this drawing", HasBlock(db, LibraryBlock));
        report.Check("the insert's layer was created", HasLayer(db, "S-COLUMN"));

        var reference = engine.Journal.At(1)?.ObjectsAfter.FirstOrDefault() ?? "";
        report.Note(DescribeReference(db, reference));
        var nameless = NamelessDefinitions(db);
        report.Check("the import left no nameless block definition behind",
            nameless.Count == 0,
            nameless.Count == 0 ? "" : string.Join(", ", nameless));
        report.Check("the step recorded the block reference as its own object",
            BlockOf(db, reference) == LibraryBlock,
            $"handle={reference}, block={BlockOf(db, reference) ?? "(not a block reference)"}");

        // Rotated *and* scaled, so the extents are the block's 400 x 400 turned by 30
        // degrees: 600 cos 30 + 800 sin 30 across, 600 sin 30 + 800 cos 30 up.
        var turned = ExtentsOf(db, reference);
        report.Check("the rotated insert has real geometry behind it",
            turned is { } box && Math.Abs(box.Width - 919.6) < 0.5 && Math.Abs(box.Height - 992.8) < 0.5,
            turned is null
                ? "no extents"
                : $"{turned.Value.Width:0.##} x {turned.Value.Height:0.##} mm, wanted 919.62 x 992.82");

        var rotation = Read<double?>(db, reference, entity => entity is BlockReference r ? r.Rotation : null, null);
        report.Check("the insert kept its rotation",
            rotation is { } angle && Math.Abs(angle - Math.PI / 6) < 1e-9,
            $"{rotation * 180 / Math.PI:0.##} degrees, wanted 30");

        // --- a second insert must reuse the definition ----------------------
        var upright = engine.Apply("place_column", "column 2", null,
        [
            new Primitive
            {
                Kind = PrimitiveKind.Insert,
                Points = [8000, 2500],
                Block = LibraryBlock,
                Source = library,
                ScaleX = 1.5,
                ScaleY = 2.0,
                Layer = "S-COLUMN",
            },
        ]);
        report.Check("a second insert of the same block succeeds", upright.Ok, upright.Message);
        report.Check("it did not import a second definition",
            BlockCount(db, LibraryBlock) == 1, $"{BlockCount(db, LibraryBlock)} definition(s)");

        var second = engine.Journal.At(2)?.ObjectsAfter.FirstOrDefault() ?? "";
        report.Check("both references use the one definition",
            BlockOf(db, second) == LibraryBlock,
            $"block={BlockOf(db, second) ?? "(not a block reference)"}");

        // Scaled only, not rotated, so the measured extents are the scale factors times
        // the block's own 400 x 400.
        var extents = ExtentsOf(db, second);
        report.Check("the imported geometry arrived, scaled",
            extents is { } size && Math.Abs(size.Width - 600) < 0.5 && Math.Abs(size.Height - 800) < 0.5,
            extents is null ? "no extents" : $"{extents.Value.Width:0.##} x {extents.Value.Height:0.##} mm, wanted 600 x 800");

        // --- rolling back must leave the definition -------------------------
        var reverted = engine.RollbackTo(1);
        report.Check("rolling back across the second insert succeeds", reverted.Ok, reverted.Message);
        report.Check("the rollback removed the reference it had created",
            CountPresent(db, [second]) == 0, $"present={CountPresent(db, [second])}");
        report.Check("the rollback left the definition standing",
            HasBlock(db, LibraryBlock) && BlockCount(db, LibraryBlock) == 1,
            $"{BlockCount(db, LibraryBlock)} definition(s)");
        report.Check("the surviving step's reference still resolves",
            BlockOf(db, reference) == LibraryBlock);

        var everything = engine.RollbackTo(0, force: true);
        report.Check("rolling back every step succeeds", everything.Ok, everything.Message);
        report.Check("no reference is left in the drawing",
            CountPresent(db, [reference, second]) == 0);
        report.Check("the definition survives rolling back everything", HasBlock(db, LibraryBlock));

        var again = engine.Apply("place_column", "column again", null,
        [
            new Primitive
            {
                Kind = PrimitiveKind.Insert,
                Points = [3000, 2500],
                Block = LibraryBlock,
                Source = library,
                Rotation = 30,
                ScaleX = 1.5,
                ScaleY = 2.0,
                Layer = "S-COLUMN",
            },
        ]);
        report.Check("an insert after a full rollback reuses the definition rather than re-importing",
            again.Ok && BlockCount(db, LibraryBlock) == 1,
            $"{BlockCount(db, LibraryBlock)} definition(s); {again.Message}");

        // --- and the refusals, which must be refusals rather than quiet no-ops ---
        var missing = engine.Apply("place_column", "missing", null,
        [
            new Primitive
            {
                Kind = PrimitiveKind.Insert,
                Points = [0, 0],
                Block = "NO-SUCH-BLOCK",
                Source = library,
            },
        ]);
        report.Check("inserting a block the library has not got is refused, naming both",
            !missing.Ok && missing.Message.Contains("NO-SUCH-BLOCK")
            && missing.Message.Contains("acadmcp-selftest-library"),
            missing.Message);

        var noLibrary = engine.Apply("place_column", "no library", null,
        [
            new Primitive { Kind = PrimitiveKind.Insert, Points = [0, 0], Block = "ALSO-MISSING" },
        ]);
        report.Check("inserting an unknown block without naming a library is refused",
            !noLibrary.Ok && noLibrary.Message.Contains("ALSO-MISSING"), noLibrary.Message);

        var badProperty = engine.Apply("place_column", "bad property", null,
        [
            new Primitive
            {
                Kind = PrimitiveKind.Insert,
                Points = [0, 0],
                Block = LibraryBlock,
                Source = library,
                Properties = new JsonObject { ["no-such-parameter"] = 1 },
            },
        ]);
        report.Check("setting a property the block has not got is refused, not ignored",
            !badProperty.Ok && badProperty.Message.Contains("no-such-parameter"),
            badProperty.Message);

        var collapsed = engine.Apply("place_column", "collapsed", null,
        [
            new Primitive
            {
                Kind = PrimitiveKind.Insert,
                Points = [0, 0],
                Block = LibraryBlock,
                Source = library,
                ScaleX = 0,
            },
        ]);
        report.Check("inserting at a zero scale is refused",
            !collapsed.Ok && collapsed.Message.Contains("invisible"), collapsed.Message);

        // Leave the drawing as this section found it: no steps, no stray references.
        engine.RollbackTo(0, force: true);
        engine.Reset();
        report.Check("the block checks left no steps behind", engine.Journal.IsEmpty);
    }

    /// <summary>
    /// Definitions with no name at all. Junk: an import can leave one behind, and nothing
    /// can ever reference it by name afterwards.
    /// </summary>
    private static List<string> NamelessDefinitions(Database db)
    {
        using var tr = db.TransactionManager.StartTransaction();
        var table = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var nameless = new List<string>();
        foreach (var id in table)
        {
            if (tr.GetObject(id, OpenMode.ForRead) is BlockTableRecord definition
                && string.IsNullOrEmpty(definition.Name))
            {
                nameless.Add($"{definition.Cast<ObjectId>().Count()} entities, "
                    + $"anonymous={definition.IsAnonymous}");
            }
        }
        tr.Commit();
        return nameless;
    }

    /// <summary>Whether the drawing defines this block.</summary>
    private static bool HasBlock(Database db, string name)
    {
        using var tr = db.TransactionManager.StartTransaction();
        var table = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var has = table.Has(name);
        tr.Commit();
        return has;
    }

    /// <summary>Whether the drawing has this layer.</summary>
    private static bool HasLayer(Database db, string name)
    {
        using var tr = db.TransactionManager.StartTransaction();
        var table = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
        var has = table.Has(name);
        tr.Commit();
        return has;
    }

    /// <summary>
    /// Definitions whose name starts with this, so that a duplicate import would show up
    /// as a second entry rather than passing for the original.
    /// </summary>
    private static int BlockCount(Database db, string prefix)
    {
        using var tr = db.TransactionManager.StartTransaction();
        var table = (BlockTable)tr.GetObject(db.BlockTableId, OpenMode.ForRead);
        var count = 0;
        foreach (var id in table)
        {
            if (tr.GetObject(id, OpenMode.ForRead) is BlockTableRecord definition
                && definition.Name.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                count++;
            }
        }
        tr.Commit();
        return count;
    }

    /// <summary>The name of the definition a block reference uses, or null if it is not one.</summary>
    private static string? BlockOf(Database db, string handle) => Read<string?>(db, handle,
        entity => entity is not BlockReference reference || reference.BlockTableRecord.IsNull
            ? null
            : (reference.BlockTableRecord.GetObject(OpenMode.ForRead) as BlockTableRecord)?.Name,
        null);

    /// <summary>What a reference resolves to, for the log.</summary>
    private static string DescribeReference(Database db, string handle) => Read(db, handle,
        entity => entity is not BlockReference reference
            ? $"{entity.GetRXClass().Name} (not a block reference)"
            : $"reference: Name='{reference.Name}' dynamic={reference.IsDynamicBlock}, definition="
                + (reference.BlockTableRecord.IsNull
                    ? "none"
                    : reference.BlockTableRecord.GetObject(OpenMode.ForRead) is BlockTableRecord definition
                        ? $"'{definition.Name}' anonymous={definition.IsAnonymous}"
                        : "unreadable"),
        "unreadable");

    /// <summary>The width and height a reference actually occupies, in millimetres.</summary>
    private static (double Width, double Height)? ExtentsOf(Database db, string handle) =>
        Read<(double, double)?>(db, handle, entity =>
        {
            var extents = entity.GeometricExtents;
            return (extents.MaxPoint.X - extents.MinPoint.X, extents.MaxPoint.Y - extents.MinPoint.Y);
        }, null);

    /// <summary>Read one entity by handle and hand it to a reader, inside a transaction.</summary>
    /// <remarks>
    /// A reader rather than the entity itself: a database object is only usable inside the
    /// transaction that opened it, so anything worth keeping has to be copied out before the
    /// transaction ends.
    /// </remarks>
    private static T Read<T>(Database db, string handle, Func<Entity, T> read, T fallback)
    {
        if (!long.TryParse(handle, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var value)
            || !db.TryGetObjectId(new Handle(value), out var id) || id.IsNull || id.IsErased)
        {
            return fallback;
        }

        using var tr = db.TransactionManager.StartTransaction();
        var result = tr.GetObject(id, OpenMode.ForRead) is Entity entity && !entity.IsErased
            ? read(entity)
            : fallback;
        tr.Commit();
        return result;
    }

    /// <summary>
    /// Time the operations whose cost grows with the number of steps, at two sizes.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Reported rather than asserted: the numbers are the point, and a busy machine should
    /// not fail a build over them. Two sizes, because one number cannot show how the cost
    /// grows — and how it grows is the whole question. Saving rewrites every record, so the
    /// per-step cost rises with the length of the journal; whether that matters depends on
    /// how fast it rises.
    /// </para>
    /// <para>
    /// Both costs are felt in practice: the palette re-reads the journal once a second, and
    /// every mutation saves.
    /// </para>
    /// </remarks>
    private static void MeasureAtScale(StepEngine engine, Report report)
    {
        var clock = new Stopwatch();
        foreach (var target in (int[])[200, 400])
        {
            var from = engine.Journal.Count;
            clock.Restart();
            for (var i = from + 1; i <= target; i++)
            {
                engine.Apply("bench_step", $"bench {i}", null,
                [
                    new Primitive
                    {
                        Kind = PrimitiveKind.Line,
                        Points = [i * 10, 0, i * 10, 500],
                        Layer = "A-WALL",
                    },
                ]);
            }
            var built = clock.ElapsedMilliseconds;

            clock.Restart();
            engine.Reload();
            var reload = clock.ElapsedMilliseconds;

            clock.Restart();
            engine.SetNote(target, "timed");
            var mutate = clock.ElapsedMilliseconds;

            var added = target - from;
            report.Note($"scale at {target} steps: {added} steps in {built} ms "
                + $"({(double)built / added:0.#} ms each), full re-read {reload} ms, "
                + $"one mutation {mutate} ms");
        }

        report.Check($"the journal holds {engine.Journal.Count} steps under load",
            engine.Journal.Count == 400, $"count={engine.Journal.Count}");
        CheckRoundTrip(engine, report, "400 steps");

        // Leave the drawing and the journal as they were found, or the checks after this one
        // would be counting the benchmark's steps.
        engine.RollbackTo(0, force: true);
        engine.Reset();
    }

    /// <summary>
    /// What was read off a text entity, copied out of its transaction.
    /// </summary>
    /// <remarks>
    /// A snapshot rather than the entity: a database object is only usable inside
    /// the transaction that opened it, so handing the caller a live <c>DBText</c>
    /// would fail the moment the check touched it.
    /// </remarks>
    private sealed record TextFacts(string Content, string Style, double Height, string Font, string BigFont);

    /// <summary>How the first text entity with this content was actually drawn.</summary>
    private static TextFacts? DescribeText(Database db, string content)
    {
        using var tr = db.TransactionManager.StartTransaction();
        var model = (BlockTableRecord)tr.GetObject(
            SymbolUtilityServices.GetBlockModelSpaceId(db), OpenMode.ForRead);

        foreach (var id in model)
        {
            if (tr.GetObject(id, OpenMode.ForRead) is not DBText text
                || text.IsErased
                || text.TextString != content)
            {
                continue;
            }

            var style = text.TextStyleName;
            var facts = new TextFacts(
                text.TextString, style, text.Height, "", "");
            var (font, bigFont) = ReadStyleFonts(tr, db, style);
            tr.Commit();
            return facts with { Font = font, BigFont = bigFont };
        }
        tr.Commit();
        return null;
    }

    private static (string Font, string BigFont) ReadStyleFonts(Transaction tr, Database db, string name)
    {
        var table = (TextStyleTable)tr.GetObject(db.TextStyleTableId, OpenMode.ForRead);
        if (!table.Has(name))
        {
            return ("", "");
        }
        var record = (TextStyleTableRecord)tr.GetObject(table[name], OpenMode.ForRead);
        // FileName, not Font: for an SHX style this is where the .shx name lives.
        return (record.FileName, record.BigFontFileName);
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

        /// <summary>A measured number, for the log rather than for pass or fail.</summary>
        public void Note(string text) => _text.Append($"[note] {text}\n");

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
