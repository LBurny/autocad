"""Wire check: critique, finalize (both verdicts) and capture over real stdio."""


async def draw(tools):
    await tools.call("drawing_new", backend="dxf")
    await tools.call(
        "draw_wall_with_openings",
        points=[[0, 0], [4000, 0], [4000, 3000], [0, 3000]],
        closed=True,
        openings=[{"start": 1000, "width": 900, "kind": "door", "tag": "M0921"}],
    )
    await tools.call("draw_opening", kind="door", at=[1000, 0], rotation=0, width=900)

    crit = (await tools.call("drawing_check", check="critique"))["checks"]["critique"]
    assert crit["counts"]["error"] == 0, crit["issues"]
    print("sandbox: clean drawing critiques clean")

    await tools.call("draw_opening", kind="door", at=[0, 9000], rotation=0, width=900)
    crit = (await tools.call("drawing_check", check="critique", focus="door_without_opening"))[
        "checks"
    ]["critique"]
    assert crit["counts"]["error"] == 1, crit
    try:
        await tools.call("drawing_finalize")
    except Exception as exc:
        assert "not finished" in str(exc), exc
        print("sandbox: finalize refused the floating door over the wire")
    else:
        raise AssertionError("finalize passed a drawing with a floating door")

    await tools.call("drawing_undo")  # the floating door
    fin = await tools.call("drawing_finalize")
    assert fin["ok"] and fin["verdict"] == "matches", fin
    print("sandbox: after undo, finalize passes:", {k: fin[k] for k in ("rooms", "openings")})

    hist = await tools.call("drawing_history")
    door_step = [
        s["index"]
        for s in hist["steps"]
        if s["op"] == "draw_door" and s.get("state") == "applied"
    ]
    cap = await tools.call("element_capture", steps=door_step, key="door900")
    placed = await tools.call("place_element", type="door900", at=[9000, 0], tag=False)
    assert placed["placed"] == 1
    print("sandbox: captured step", door_step, "as", cap["block"], "and placed it")
