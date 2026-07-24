"""解析测试页 /parse：粘贴一个种子标题，实时看解析出的组名/番名/季/集/候选名/是否合集/采集判定。

用来核对字幕组命名、验证解析规则改动会不会误伤大组（粘 ANi 的标题看名字还对不对即可）。
页内带『多括号回退捕获』开关，可就地开关后立即看效果（全局生效，等同设置页那个开关）。顶栏『解析测试』进入。
"""
from nicegui import ui

import config
from sources.parse import candidate_names, is_batch, parse_multibracket, parse_title
from .layout import ep_str, frame

# (标签, 标题)——覆盖：大组正常 / 各种集数特例 / 合集 / 多括号四种
_EXAMPLES = [
    ("大组·正常", "[ANi] 葬送的芙莉莲 - 07 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]"),
    ("大组·续作季", "[Lilith-Raws] 药屋少女的呢喃 第二季 - 15 [Baha][WEB-DL][1080p][AVC AAC][CHT][MP4]"),
    ("版本号 v2", "[绿茶字幕组&LoliHouse] 世界在起舞 / The World Is Dancing - 01v2 [WebRip 1080p HEVC-10bit AAC]"),
    ("中文数字集", "[某字幕组] 某番 第二十三话 [1080p][简繁内封]"),
    ("特例·。44:", "[LoliHouse] 死亡遊戯で飯を食う。44:CLOUDY BEACH [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"),
    ("BD盘(合集)", "[BDMV] 葬送のフリーレン | Sousou no Frieren | Frieren: Beyond Journey's End"),
    ("整季合集", "[Nekomoe kissaten] Spy x Family [38-50 合集][1080p]"),
    ("多括号·斜杠", "[沸羊羊字幕组中日双语][葬送的芙莉莲/Sousou no Frieren/葬送のフリーレン][第19集周密的计划][2160P+1080P][Crunchyroll]"),
    ("多括号·悠哈", "【悠哈璃羽字幕社】[碧蓝航线 微速前进 S2_Azur Lane - Bisoku Zenshin! S2][03][x264 1080p][CHT]"),
    ("多括号·国漫", "[GM-Team][国漫][遮天][Shrouding the Heavens][2023][172][AVC][GB][1080P]"),
    ("多括号·Skymoon", "[Skymoon-Raws][One Piece 海贼王][1170][ViuTV][WEB-RIP][CHT][SRT][1080p][MKV]"),
]


def _ep_label(e) -> str:
    if e == -1:
        return "特别篇 (-1)"
    if e == -2:
        return "未识别 (-2)——不会自动下，进『待识别』"
    return f"第 {ep_str(e)} 集"


def _ep_short(e) -> str:
    """徽标用的短集号；完整解释放 tooltip。"""
    if e == -1:
        return "特别篇"
    if e == -2:
        return "未识别"
    return f"第 {ep_str(e)} 集"


@ui.page("/parse")
def parse_test_page():
    with frame("parse"):
        ui.label("解析测试").classes("text-2xl font-bold")
        ui.label("粘贴一条 RSS 里的种子标题，实时看解析结果。也用来验证解析规则会不会误伤大组"
                 "——粘 ANi/Lilith 的标题，看番名/集数是否照旧。").classes("text-xs text-gray-400 mb-2")

        with ui.card().classes("w-full gap-3"):
            inp = ui.input("种子标题", placeholder="粘贴一条种子名…").props(
                "dense outlined clearable autofocus").classes("w-full")
            with ui.row().classes("items-center gap-2 flex-wrap"):
                sw = ui.switch("多括号回退捕获", value=config.ANIME_MULTIBRACKET_PARSE).props("dense")
                ui.label("多括号命名：番名也塞在 [ ] 括号块里（如 [组][番名/别名][集数][分辨率]），"
                         "常规解析取不到番名；打开则从括号块回退捞出番名。全局生效，等同设置页那个开关").classes(
                    "text-xs text-gray-500")

        @ui.refreshable
        def result():
            raw = (inp.value or "").strip()
            if not raw:
                return
            group, name, season, episode = parse_title(raw)
            batch = is_batch(raw)
            cands = candidate_names(raw)
            mb_on = config.ANIME_MULTIBRACKET_PARSE
            mb = parse_multibracket(raw) if (not name and not batch) else None
            recovered = False
            if mb and mb_on:                    # 开关开：实际会用多括号回退结果
                name, cands, recovered = mb[0], mb[1], True

            if batch:
                verdict, icon, color = "会被丢弃：判定为合集 / BD盘 / 连续集范围", "cancel", "red"
            elif recovered:
                verdict, icon, color = "会被采集入库（多括号回退捕获恢复了番名）", "check_circle", "green"
            elif not name and mb:
                verdict, icon, color = (
                    "现在会被丢弃；但『多括号回退』可恢复为 “" + mb[0] + "”——打开上面的开关即可捕获",
                    "info", "blue")
            elif not name:
                verdict, icon, color = "会被丢弃：番名解析为空（多括号回退也没把握，落『待识别』）", "warning", "orange"
            else:
                verdict, icon, color = "会被采集入库", "check_circle", "green"

            with ui.card().classes("w-full gap-3"):
                # 判定横幅：图标 + 采集判定
                with ui.row().classes("items-start gap-2 no-wrap"):
                    ui.icon(icon).classes(f"text-{color}-400 text-2xl shrink-0")
                    ui.label(verdict).classes(f"text-sm font-bold text-{color}-300 min-w-0")
                ui.separator()
                # 番名（醒目）+ 合集/单集
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.label(name or "（番名为空）").classes(
                        "text-lg font-bold" + ("" if name else " text-gray-500"))
                    ui.badge("合集" if batch else "单集").props(
                        f"color={'red' if batch else 'green'}").classes("text-sm")
                # 组 / 季 / 集 徽标
                with ui.row().classes("gap-2 flex-wrap items-center"):
                    ui.badge(f"组 · {group or '—'}").props("color=blue-grey").classes("text-sm")
                    ui.badge(f"第 {season} 季").props("color=blue-grey").classes("text-sm")
                    ui.badge(f"集 · {_ep_short(episode)}").props(
                        f"color={'orange' if episode == -2 else 'blue-grey'}").classes("text-sm").tooltip(
                        _ep_label(episode))
                ui.separator()
                # bgm 候选名（识别就靠它们）
                ui.label("bgm 搜索候选名（识别就靠它们）").classes("text-xs text-gray-400")
                if cands:
                    with ui.row().classes("gap-2 flex-wrap"):
                        for c in cands:
                            ui.badge(c).props("color=indigo").classes("text-sm")
                else:
                    ui.label("（无——识别不到会进『待识别』，可手动绑定 bgm）").classes("text-sm text-gray-400")

        def _toggle():
            config.set_many({"ANIME_MULTIBRACKET_PARSE": "true" if sw.value else "false"})
            result.refresh()
            ui.notify(f"多括号捕获已{'开启' if sw.value else '关闭'}（全局生效）",
                      type="positive" if sw.value else "info")

        sw.on_value_change(_toggle)
        inp.on_value_change(lambda: result.refresh())

        def _fill(text):
            inp.value = text
            result.refresh()

        ui.label("点一个示例填入：").classes(
            "text-xs text-gray-500 mt-4 mb-1")
        with ui.column().classes("gap-0 w-full"):
            for tag, ex in _EXAMPLES:
                with ui.row().classes(
                        "items-center gap-2 no-wrap w-full cursor-pointer rounded px-2 py-1 "
                        "hover:bg-white/5 transition-colors").on("click", lambda e=ex: _fill(e)):
                    ui.badge(tag).props("color=blue").classes("shrink-0")
                    ui.label(ex).classes("text-xs text-gray-400 break-all")
        result()
