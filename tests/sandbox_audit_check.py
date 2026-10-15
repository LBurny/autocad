"""Wire check: the instructions arrive at initialize and the audit log fires."""


async def draw(tools):
    init = await tools.session.initialize()
    instructions = getattr(init, "instructions", "") or ""
    assert "draw_wall_with_openings" in instructions, "no instructions on initialize"
    print("sandbox: initialize carried", len(instructions), "chars of instructions")
    # Two tool calls — the server's stderr (inherited by the driver) must show
    # two TOOL OK lines from the audit middleware; grep the run output for them.
    await tools.call("drawing_new", backend="dxf")
    await tools.call("drawing_status")
    print("sandbox: two tool calls made; check the server stderr above for TOOL OK")
