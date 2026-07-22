"""解析测试页 /parse：粘贴一个种子标题，实时看解析出的组名/番名/季/集/候选名/是否合集/采集判定。

用来核对字幕组命名、验证解析规则改动会不会误伤大组（粘 ANi 的标题看名字还对不对即可）。
顶栏『解析测试』进入。
"""
from nicegui import ui

from sources.parse import candidate_names, is_batch, parse_title
from .layout import ep_str, frame

_EXAMPLES = [
    "[ANi] 葬送的芙莉莲 - 07 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]",
    "[Lilith-Raws] 药屋少女的呢喃 第二季 - 15 [Baha][WEB-DL][1080p][AVC AAC][CHT][MP4]",
    "[绿茶字幕组&LoliHouse] 世界在起舞 / The World Is Dancing - 01v2 [WebRip 1080p HEVC-10bit AAC]",
    "【悠哈璃羽字幕社】[碧蓝航线 微速前进 S2_Azur Lane - Bisoku Zenshin! S2][03][x264 1080p][CHT]",
    "[GM-Team][国漫][遮天][Shrouding the Heavens][2023][172][AVC][GB][1080P]",
    "[Nekomoe kissaten] Spy x Family [38-50 合集][1080p]",
]


def _ep_label(e) -> str:
    if e == -1:
        return "特别篇 (-1)"
    if e == -2:
        return "未识别 (-2)——不会自动下，进『待识别』"
    return f"第 {ep_str(e)} 集"


@ui.page("/parse")
def parse_test_page():
    with frame("parse"):
        ui.label("解析测试").classes("text-2xl font-bold")
        ui.label("粘贴一条 RSS 里的种子标题，实时看解析结果。也用来验证解析规则改动会不会误伤大组"
                 "——粘 ANi/Lilith 的标题，看番名/集数是否照旧。").classes("text-xs text-gray-400 mb-2")

        inp = ui.input("种子标题", placeholder="粘贴一条种子名…").classes("w-full")

        @ui.refreshable
        def result():
            raw = (inp.value or "").strip()
            if not raw:
                ui.label("（在上面粘贴一个标题）").classes("text-gray-500 p-4")
                return
            group, name, season, episode = parse_title(raw)
            batch = is_batch(raw)
            cands = candidate_names(raw)
            if batch:
                verdict, icon, color = "会被丢弃：判定为合集 / BD盘 / 连续集范围", "cancel", "red"
            elif not name:
                verdict, icon, color = "会被丢弃：番名解析为空（多括号格式，暂未支持）", "warning", "orange"
            else:
                verdict, icon, color = "会被采集入库", "check_circle", "green"

            with ui.card().classes("w-full gap-1"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon(icon).classes(f"text-{color}-400")
                    ui.label(verdict).classes(f"font-bold text-{color}-300")
                ui.separator()
                with ui.grid(columns=2).classes("gap-x-8 gap-y-1"):
                    for k, v in [("组名", group or "—"), ("番名", name or "（空）"),
                                 ("季", f"第 {season} 季"), ("集", _ep_label(episode)),
                                 ("是否合集", "是" if batch else "否")]:
                        ui.label(k).classes("text-xs text-gray-400")
                        ui.label(str(v))
                ui.label("bgm 搜索候选名（识别就靠它们）").classes("text-xs text-gray-400 mt-1")
                if cands:
                    for c in cands:
                        ui.label("· " + c).classes("text-sm")
                else:
                    ui.label("（无——识别不到会进『待识别』，可手动绑定 bgm）").classes("text-sm text-orange-300")

        inp.on_value_change(lambda: result.refresh())

        def _fill(text):
            inp.value = text
            result.refresh()

        ui.label("点一个示例填入（前两条是大组，看名字/集数是否解析正确）：").classes(
            "text-xs text-gray-500 mt-3")
        with ui.column().classes("gap-1 w-full"):
            for ex in _EXAMPLES:
                ui.label(ex).classes(
                    "cursor-pointer text-xs text-blue-400 hover:underline break-all").on(
                    "click", lambda e=ex: _fill(e))
        result()
