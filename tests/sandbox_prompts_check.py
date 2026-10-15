"""Wire check: the prompts reach a real MCP client over stdio."""


async def draw(tools):
    prompts = await tools.session.list_prompts()
    names = sorted(p.name for p in prompts.prompts)
    print("sandbox prompts:", names)
    assert names == ["drawing_review", "floor_plan", "headless_to_live"], names
    got = await tools.session.get_prompt("floor_plan", {"building_name": "Sandbox Tower"})
    text = got.messages[0].content.text
    assert "Sandbox Tower" in text and "draw_wall_with_openings" in text
    print("sandbox: floor_plan renders over the wire,", len(text), "chars")
