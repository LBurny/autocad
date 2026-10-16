"""What each tool is called in AutoCAD, and what a person would call it.

A draughtsman searches for `FILLET`; that word is in no tool name and no description, so the
tools were not ranked badly — they were unsearchable. The fix is data: one record per tool
naming the AutoCAD commands it stands for, and the phrases — Chinese among them — that
somebody would actually type.

This module is deliberately not a search engine. At 48 tools the whole surface fits in a
client's context, so nothing needs ranking; what is needed is a way from the word a person
used to the tool that does it, which is `lookup` and the `term` argument of `drawing_help`.
`tests/test_terms.py` refuses to let a tool exist without a record here — the same rule the
catalogue follows for an entry whose block the library has not got, and the journal for a
record it cannot read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Anything at or above U+2E80 is outside the Latin ranges an AutoCAD command name lives in.
#: The bound is the one `backends/dxf.py`'s `_has_cjk` and the CJK text style already use.
CJK_PATTERN = re.compile(r"[\u2e80-\u9fff\uff00-\uffef]")


@dataclass(frozen=True, slots=True)
class Term:
    """One tool's vocabulary.

    `cad` is what AutoCAD calls it — the command a draughtsman would reach for — and
    `phrases` is what a person calls it, in English and in Chinese. Empty `cad` is the
    honest answer for a tool that computes something AutoCAD has no single command for.
    """

    cad: tuple[str, ...]
    phrases: tuple[str, ...]


#: Commands two tools genuinely drive, each for its own half of the job. Deliberately tiny:
#: a duplicated command is usually a copy-paste mistake, and the gate in `tests/test_terms.py`
#: fails on one rather than letting it pass quietly. Each entry here is a command that two
#: tools legitimately stand for — MLINE for the wall run and the run an opening is cut into,
#: NEW for `drawing_new` and the `template_list` of what NEW starts from, and WBLOCK for the
#: capture and the publish of what it captured.
SHARED_CAD: tuple[str, ...] = ("MLINE", "NEW", "WBLOCK")


#: Keyed by tool name, in the order the tools appear in `server.py`.
TOOL_TERMS: dict[str, Term] = {
    # --- session and document ----------------------------------------------
    "drawing_help": Term(
        cad=(),
        phrases=("help", "工具", "怎么用", "使用说明", "有哪些工具"),
    ),
    "drawing_status": Term(
        cad=("STATUS",),
        phrases=("status", "状态", "当前图纸", "有多少实体"),
    ),
    "drawing_new": Term(
        cad=("NEW", "QNEW"),
        phrases=("new drawing", "新建图纸", "空白图", "从头开始"),
    ),
    "drawing_open": Term(
        cad=("OPEN",),
        phrases=("open drawing", "打开图纸", "载入 dxf", "读取已有图纸"),
    ),
    "template_list": Term(
        cad=("NEW",),
        phrases=("模板", "样板", "图框模板"),
    ),
    "drawing_save": Term(
        cad=("SAVEAS", "QSAVE"),
        phrases=("save", "保存", "另存为", "导出 dwg"),
    ),
    "drawing_screenshot": Term(
        cad=("EXPORT",),
        phrases=("screenshot", "截图", "出图看看", "渲染成图片", "预览"),
    ),
    "drawing_sheet": Term(
        cad=("PAGESETUP", "LAYOUT"),
        phrases=("sheet", "图纸", "图框", "标题栏", "幅面", "比例尺", "图纸设置", "加图框"),
    ),
    "drawing_plot_pdf": Term(
        cad=("PLOT", "PUBLISH", "EXPORTPDF"),
        phrases=("plot", "出图", "打印", "出 pdf", "导出图纸", "打图", "晒图"),
    ),
    "acad_connect": Term(
        cad=(),
        phrases=("connect", "连接 AutoCAD", "接上 autocad", "实时绘图"),
    ),
    "acad_zoom": Term(
        cad=("ZOOM",),
        phrases=("zoom", "缩放", "全图显示", "放大到范围"),
    ),
    # --- the journal --------------------------------------------------------
    "drawing_undo": Term(
        cad=("UNDO",),
        phrases=(
            "undo",
            "撤销",
            "退回一步",
            "画错了",
            "rollback",
            "回滚",
            "退回到某一步",
            "撤到指定步骤",
        ),
    ),
    "drawing_redo": Term(
        cad=("REDO",),
        phrases=("redo", "重做", "恢复撤销", "再画一次"),
    ),
    "drawing_history": Term(
        cad=(),
        phrases=("history", "历史", "画了什么", "步骤列表"),
    ),
    "drawing_summary": Term(
        cad=("AREA",),
        phrases=("summary", "汇总", "图纸说明", "统计", "面积表"),
    ),
    "drawing_check": Term(
        cad=("AUDIT", "CHECKSTANDARDS", "STANDARDS"),
        phrases=(
            "check",
            "verify",
            "检查图纸",
            "核对",
            "查错",
            "图纸和记录对不对",
            "critique",
            "构件核查",
            "组件检查",
            "门画对了吗",
            "标准检查",
            "图层规范",
            "制图标准",
        ),
    ),
    "drawing_finalize": Term(
        cad=(),
        phrases=("finalize", "收官", "验收", "完成检查", "图纸做完了吗"),
    ),
    "drawing_replay": Term(
        cad=(),
        phrases=("replay", "重放到 autocad", "把 dxf 画到 autocad", "复现"),
    ),
    # --- layers, styles and reading ----------------------------------------
    "layer_create": Term(
        cad=("LAYER",),
        phrases=("layer", "图层", "建图层", "新建图层"),
    ),
    "set_text_style": Term(
        cad=("STYLE",),
        phrases=("text style", "文字样式", "字体", "中文乱码"),
    ),
    "listing_entities": Term(
        cad=("QSELECT",),
        phrases=("list entities", "列出实体", "有多少图元", "查图层上的东西"),
    ),
    # --- the drawing primitives --------------------------------------------
    "draw_line": Term(
        cad=("LINE",),
        phrases=("line", "直线", "画根线", "线段"),
    ),
    "draw_polyline": Term(
        cad=("PLINE",),
        phrases=("polyline", "多段线", "折线", "一次画很多段"),
    ),
    "draw_rect": Term(
        cad=("RECTANG",),
        phrases=("rectangle", "矩形", "画个方框", "长方形"),
    ),
    "draw_circle": Term(
        cad=("CIRCLE",),
        phrases=("circle", "圆", "画个圆", "圆弧"),
    ),
    # --- the architectural operations --------------------------------------
    "draw_wall": Term(
        cad=("MLINE",),
        phrases=("wall", "墙", "墙体", "画墙", "隔墙"),
    ),
    "draw_wall_with_openings": Term(
        cad=("MLINE",),
        phrases=(
            "wall with openings",
            "墙和洞口",
            "开洞的墙",
            "带门窗的墙",
            "围合",
            "外墙一圈",
            "卷帘门",
        ),
    ),
    "draw_room": Term(
        cad=("BOUNDARY",),
        phrases=(
            "room",
            "房间",
            "房间面积",
            "标注房间",
            "使用面积",
            "卫生间",
            "办公室",
            "走廊",
            "楼梯间",
        ),
    ),
    "draw_opening": Term(
        cad=(),
        phrases=(
            "door",
            "门",
            "画门",
            "单扇门",
            "门洞",
            "卷帘门",
            "卷帘",
            "防火门",
            "window",
            "窗",
            "画窗",
            "窗户",
            "窗洞",
            "幕墙",
            "百叶",
        ),
    ),
    "draw_column": Term(
        cad=("SOLID",),
        phrases=("column", "承重柱", "结构柱", "柱子", "框架柱"),
    ),
    "draw_core": Term(
        cad=(),
        phrases=(
            "stair",
            "楼梯",
            "画楼梯",
            "梯段",
            "踏步",
            "lift",
            "电梯",
            "电梯井",
            "升降梯",
            "竖井",
            "电梯间",
            "候梯厅",
            "equipment",
            "设备",
            "机房设备",
            "强弱电井",
            "水井",
            "管井",
            "水泵",
            "设备基础",
            "配电柜",
        ),
    ),
    "draw_axis_grid": Term(
        cad=("XLINE",),
        phrases=("axis grid", "轴网", "定位轴线", "柱网", "画轴线"),
    ),
    # --- annotation ---------------------------------------------------------
    "add_text": Term(
        cad=("TEXT", "MTEXT"),
        phrases=("text", "文字", "写字", "加文字", "注写"),
    ),
    "add_dimension": Term(
        cad=("DIMLINEAR", "DIMALIGNED"),
        phrases=("dimension", "标注", "尺寸", "标尺寸", "尺寸链"),
    ),
    "add_leader_note": Term(
        cad=("MLEADER", "QLEADER"),
        phrases=("leader", "引线", "引出标注", "注释引线", "做法标注"),
    ),
    "add_hatch": Term(
        cad=("HATCH",),
        phrases=("hatch", "填充", "剖面线", "打填充", "材质图例"),
    ),
    # --- the element catalogue ---------------------------------------------
    "element_types": Term(
        cad=(),
        phrases=("element types", "构件库", "图块库", "有哪些构件", "图库"),
    ),
    "library_list": Term(
        cad=(),
        phrases=("library", "图块", "动态块", "dwg 图库", "库里有什么"),
    ),
    "place_element": Term(
        cad=("INSERT", "DIVIDE", "MEASURE", "ARRAY"),
        phrases=(
            "place element",
            "插入图块",
            "放置构件",
            "放门",
            "放窗",
            "place along",
            "沿墙布置",
            "等分布置",
            "一排窗",
            "按间距排",
            "place grid",
            "阵列",
            "柱网布柱",
            "按轴网布置",
            "成排布置",
            # The library's own members, reachable only through this tool: a draughtsman
            # types 钢梁 or 焊接符号, and the honest answer is "place_element", because that
            # is what draws them.
            "立柱",
            "钢立柱",
            "钢梁",
            "槽钢",
            "工字钢",
            "型钢",
            "焊接符号",
            "灯具",
        ),
    ),
    "element_capture": Term(
        cad=("BLOCK", "WBLOCK"),
        phrases=("capture element", "收编构件", "做成块", "创建块", "沉淀构件", "画一次以后用"),
    ),
    "element_publish": Term(
        cad=("WBLOCK",),
        phrases=("发布元件", "入库", "存进组件库"),
    ),
    "catalog_suggest": Term(
        cad=(),
        phrases=("沉淀建议", "重复", "建议入库"),
    ),
    # --- the step journal and the plugin -----------------------------------
    "step_begin": Term(
        cad=(),
        phrases=("step begin", "开始一步", "手动分步", "开一个步骤"),
    ),
    "step_commit": Term(
        cad=(),
        phrases=("step commit", "结束这一步", "提交步骤", "落定"),
    ),
    "step_list": Term(
        cad=(),
        phrases=("step list", "步骤", "看步骤", "有哪些步骤"),
    ),
    "step_control": Term(
        cad=(),
        phrases=("step control", "步骤控制", "回滚步骤", "接受这一步", "重跑"),
    ),
    "plugin_status": Term(
        cad=(),
        phrases=("plugin", "插件状态", "面板", "插件在不在"),
    ),
    "plugin_install": Term(
        cad=(),
        phrases=("install plugin", "安装插件", "装面板", "部署插件"),
    ),
}


def terms_for(name: str) -> Term | None:
    """The record for a tool, or None — the caller decides what a missing record means."""
    return TOOL_TERMS.get(name)


def all_terms() -> dict[str, Term]:
    return dict(TOOL_TERMS)


def lookup(query: str) -> list[tuple[str, str]]:
    """Tools matching `query`, as (tool, what matched), best first and deterministic.

    Ranked by how strong the match is — an exact AutoCAD command, then a phrase that *is* the
    query, then a phrase the query *contains*, then a phrase containing the query, then a
    substring of a tool's own name — and ties are broken by tool name, so the answer never
    depends on dictionary order. `what` is one of "cad", "phrase" or "name".

    The contained-in-the-query direction is not a nicety: a Chinese compound carries the word
    inside it. 卷帘门 is a door, and searching for the compound has to reach `draw_opening`
    without 卷帘门 having to be listed verbatim — the reverse direction alone found nothing,
    which is how a word the README itself uses came back empty.

    Command names are compared with any leading `_` or `-` stripped, because that is how the
    command line spells a transparent or no-undo variant of the same command (`-INSERT`).
    """
    text = (query or "").strip().casefold()
    if not text:
        return []
    bare = text.lstrip("_-")
    exact_cad: list[str] = []
    phrase_equal: list[str] = []
    phrase_within_query: list[str] = []
    phrase_part: list[str] = []
    in_name: list[str] = []
    for name, record in sorted(TOOL_TERMS.items()):
        folded = [phrase.casefold() for phrase in record.phrases]
        if any(command.casefold().lstrip("_-") == bare for command in record.cad):
            exact_cad.append(name)
        elif any(phrase == text for phrase in folded):
            phrase_equal.append(name)
        elif any(phrase in text for phrase in folded):
            phrase_within_query.append(name)
        elif any(text in phrase for phrase in folded):
            phrase_part.append(name)
        elif text in name.casefold():
            in_name.append(name)
    return (
        [(name, "cad") for name in exact_cad]
        + [(name, "phrase") for name in phrase_equal]
        + [(name, "phrase") for name in phrase_within_query]
        + [(name, "phrase") for name in phrase_part]
        + [(name, "name") for name in in_name]
    )
