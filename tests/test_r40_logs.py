"""日志降噪：例行公事别把 200 条的 /logs 环形缓冲冲满。

生产上每轮 6 行"源 X：N 条，新增 0"（一天 432 行）让缓冲只装得下约 8 小时，
同一条"番名解析为空"每轮两行连刷。这一组钉住：
· poll_once 每轮只记一行"本轮采集"汇总（源名、条数、有新增才带括号）；没抓到的源不进这行、失败行照记；
  一个源都没抓到时不记；
· "番名解析为空，跳过"同一标题只记一次，有上限、先进先出；
· core/anime.py 日志里的集号不再打成 6.0。
"""
import ast
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import feedparser
import pytest

from core import anime as A
from db.models import Anime, AnimeTorrent
from sources import base as B
from sources.mikan import MikanSource

_ROOT = Path(__file__).resolve().parent.parent


# ---------------- 每轮一行汇总 ----------------

class _Src:
    def __init__(self, name, hashes):
        self.name = name
        self._items = [SimpleNamespace(info_hash=h, anime_title="番") for h in hashes]

    async def fetch(self, content_cache=None):
        return list(self._items)


class _Broken:
    def __init__(self, name):
        self.name = name

    async def fetch(self, content_cache=None):
        raise RuntimeError("403 Forbidden")


def _h(prefix, i):
    return f"{prefix}{i:039x}"


@pytest.fixture
def poll(clean_tables, monkeypatch, cfg):
    """跑一轮 poll_once：源与 process_item 打桩（new_hashes 里的算新增），零网络。"""
    from core import health
    from core import worker as W

    # 连续失败计数 / seen 是进程级的：不复位的话这里的假源会留在 health 里污染后面的文件
    monkeypatch.setattr(health, "_fail", {})
    monkeypatch.setattr(health, "_seen", {})
    monkeypatch.setattr(health, "_alive", set())

    async def no_qb():
        return False
    monkeypatch.setattr(W.anime, "qb_precheck", no_qb)
    monkeypatch.setattr(W, "_build_skipped", [])
    cfg(ANIME_POLL_LAST="", ANIME_POLL_FAILED="", ANIME_POLL_OK_LAST="")

    async def run(sources, new_hashes=()):
        new = set(new_hashes)

        async def fake_process(item, known_hashes=None, qb_alive=None):
            return item.info_hash in new
        monkeypatch.setattr(W, "process_item", fake_process)
        monkeypatch.setattr(W, "build_sources", lambda: sources)
        await W.poll_once()
    return run


def _summary(caplog):
    return [r for r in caplog.records if "本轮采集" in r.getMessage()]


async def test_steady_round_logs_exactly_one_summary_line(poll, caplog):
    caplog.set_level(logging.INFO, logger="autorss")
    srcs = [_Src("ANi", [_h("a", i) for i in range(75)]),
            _Src("喵萌(简中)", [_h("b", i) for i in range(35)]),
            _Src("Mikan(后备)", [_h("c", i) for i in range(21)])]
    await poll(srcs)
    rows = _summary(caplog)
    assert len(rows) == 1 and rows[0].levelno == logging.INFO, [r.getMessage() for r in rows]
    msg = rows[0].getMessage()
    assert msg == "本轮采集：3 个源共 131 条，新增 0｜ANi 75、喵萌(简中) 35、Mikan(后备) 21", msg
    assert not [r for r in caplog.records if re.match(r"源 .+：\d+ 条，新增", r.getMessage())], \
        "每源一行的旧日志还在"


async def test_new_counts_go_in_brackets(poll, caplog):
    caplog.set_level(logging.INFO, logger="autorss")
    srcs = [_Src("ANi", [_h("a", i) for i in range(5)]),
            _Src("喵萌(简中)", [_h("b", i) for i in range(4)]),
            _Src("Mikan(后备)", [_h("c", i) for i in range(3)])]
    await poll(srcs, new_hashes=[_h("a", 0), _h("c", 1), _h("c", 2)])
    msg = _summary(caplog)[0].getMessage()
    assert msg == "本轮采集：3 个源共 12 条，新增 3｜ANi 5(+1)、喵萌(简中) 4、Mikan(后备) 3(+2)", msg


async def test_zero_item_source_stays_in_summary(poll, caplog):
    """抓到了但 0 条（白名单写错 / 全被过滤）的源要留在汇总行里写成『X 0』——
    "这个源组每轮全灭"在日志里只有这一个信号。"""
    caplog.set_level(logging.INFO, logger="autorss")
    await poll([_Src("有货r40", [_h("a", 0)]), _Src("全灭r40", [])])
    msgs = [r.getMessage() for r in _summary(caplog)]
    assert msgs == ["本轮采集：2 个源共 1 条，新增 0｜有货r40 1、全灭r40 0"], msgs


async def test_summary_is_logged_before_flush(poll, caplog, monkeypatch):
    """汇总行在放行下载之前：不然『缓冲窗口放行下载 / 已加入qB』排到它前面，分不清哪些交付属于这一轮采集。"""
    from core import worker as W

    caplog.set_level(logging.INFO, logger="autorss")
    seen_at_flush = []

    async def fake_flush(qb_alive=None):
        seen_at_flush.append(len(_summary(caplog)))
        return 0
    monkeypatch.setattr(W, "flush_ready_downloads", fake_flush)
    await poll([_Src("甲r40", [_h("a", 0)])])
    assert seen_at_flush == [1], seen_at_flush


async def test_failed_source_is_not_in_summary_but_keeps_its_own_line(poll, caplog):
    caplog.set_level(logging.INFO, logger="autorss")
    await poll([_Src("好源r40", [_h("a", 0)]), _Broken("坏源r40")])
    rows = _summary(caplog)
    assert len(rows) == 1
    msg = rows[0].getMessage()
    assert msg == "本轮采集：1 个源共 1 条，新增 0｜好源r40 1", msg
    assert "坏源r40" not in msg
    assert any("抓取失败 坏源r40" in r.getMessage() for r in caplog.records), "失败行没记"


async def test_all_sources_failed_logs_no_summary(poll, caplog):
    caplog.set_level(logging.INFO, logger="autorss")
    await poll([_Broken("坏源A_r40"), _Broken("坏源B_r40")])
    assert _summary(caplog) == []
    assert any("抓取失败 坏源A_r40" in r.getMessage() for r in caplog.records)


async def test_round_aborted_by_exception_logs_no_summary(poll, caplog, monkeypatch):
    """整轮早退（维护中 DatabaseBusy）时不记汇总：前面已处理完的源不能冒充"本轮采集"完成了。"""
    import db
    from core import worker as W

    caplog.set_level(logging.INFO, logger="autorss")
    calls = []

    async def busy_on_second(item, known_hashes=None, qb_alive=None):
        calls.append(item.info_hash)
        if item.info_hash.startswith("b"):
            raise db.DatabaseBusy("维护中")
        return False

    monkeypatch.setattr(W, "build_sources", lambda: [_Src("ANi", [_h("a", 0)]), _Src("喵萌", [_h("b", 0)])])
    monkeypatch.setattr(W, "process_item", busy_on_second)
    with pytest.raises(db.DatabaseBusy):
        await W.poll_once()
    assert calls, "桩没被调到"
    assert _summary(caplog) == []


# ---------------- 番名解析为空：同一标题只记一次 ----------------

_MIKAN_XML = """<?xml version="1.0"?><rss version="2.0"><channel><item>
<title>{title}</title><link>https://mikanani.me/Home/Episode/{h}</link>
<enclosure url="https://mikanani.me/Download/x.torrent" type="application/x-bittorrent"/>
<pubDate>Sun, 03 Aug 2026 12:00:00 -0000</pubDate></item></channel></rss>"""


def _parse(name, title):
    src = MikanSource(name, "http://x/")
    return src._parse(feedparser.parse(_MIKAN_XML.format(title=title, h="a" * 40)).entries[0])


def _empty_logs(caplog):
    return [r.getMessage() for r in caplog.records if "番名解析为空" in r.getMessage()]


def test_same_empty_name_title_logged_once_across_groups(cfg, caplog):
    cfg(ANIME_MULTIBRACKET_PARSE=False)
    caplog.set_level(logging.INFO, logger="autorss")
    t = "[组A][1080P][x264][CHS]"
    assert _parse("Mikan兜底", t) is None
    assert _parse("Mikan(后备)", t) is None
    assert _parse("Mikan兜底", t) is None
    logs = _empty_logs(caplog)
    assert logs == [f"Mikan兜底 番名解析为空，跳过：{t}"], logs


def test_different_empty_name_titles_each_logged(cfg, caplog):
    cfg(ANIME_MULTIBRACKET_PARSE=False)
    caplog.set_level(logging.INFO, logger="autorss")
    assert _parse("源", "[组A][1080P][x264][CHS]") is None
    assert _parse("源", "[组B][1080P][x264][CHS]") is None
    assert len(_empty_logs(caplog)) == 2


def test_empty_name_memory_is_bounded_fifo(cfg, caplog):
    """上限 512：超过后最老的被挤出，它再出现会再记一行；没被挤出的仍然不记。"""
    cfg(ANIME_MULTIBRACKET_PARSE=False)
    caplog.set_level(logging.INFO, logger="autorss")
    titles = [f"[组{i}][1080P][x264][CHS]" for i in range(B._EMPTY_NAME_LOGGED_MAX + 1)]
    for t in titles:
        assert _parse("源", t) is None
    assert len(B._EMPTY_NAME_LOGGED) == B._EMPTY_NAME_LOGGED_MAX
    assert len(_empty_logs(caplog)) == len(titles)
    caplog.clear()
    _parse("源", titles[-1])          # 最新的：仍记得，不再记
    assert _empty_logs(caplog) == []
    _parse("源", titles[0])           # 最老的：已被挤出，再记一次
    assert _empty_logs(caplog) == [f"源 番名解析为空，跳过：{titles[0]}"]


def test_empty_name_check_and_insert_is_atomic(monkeypatch):
    """_parse 跑在线程里（采集轮与『补齐』的并发搜索会同时解析同一条）：查 + 记必须原子。
    把"查"放慢来放大窗口——没有锁时 8 个线程都看到"没记过"、各记一行。"""
    class Slow(dict):
        def __contains__(self, k):
            r = super().__contains__(k)
            time.sleep(0.02)
            return r
    monkeypatch.setattr(B, "_EMPTY_NAME_LOGGED", Slow())
    with ThreadPoolExecutor(8) as ex:
        got = list(ex.map(lambda _: B._first_empty_name("[组][1080P]"), range(8)))
    assert got.count(True) == 1, got


def test_conftest_resets_empty_name_memory():
    """autouse 夹具每条用例前清空它，断言日志的用例不随执行顺序红绿。
    依赖同文件排在前面的用例先写过表（如 bounded_fifo）；单独跑这一条是空转。"""
    assert B._EMPTY_NAME_LOGGED == {}


# ---------------- 日志里的集号 ----------------

@pytest.mark.parametrize("ep,shown", [(6.0, "第6集"), (6.5, "第6.5集")])
def test_promote_log_prints_integer_episode_without_dot_zero(clean_tables, caplog, ep, shown):
    caplog.set_level(logging.INFO, logger="autorss")
    h = "d" * 40
    with clean_tables.get_session() as s:
        a = Anime(title="番r40", season=1, confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash=h, raw_title="t", episode=ep, status="pending",
                           priority=0, created_at=datetime.now()))
        s.commit()
    A.promote_known(SimpleNamespace(info_hash=h, priority=5, source="ANi"))
    msgs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("提权：")]
    assert len(msgs) == 1 and f" {shown} " in msgs[0], msgs


@pytest.mark.parametrize("ep,out", [(6.0, 6), (6.5, 6.5), (-1.0, -1), (-2.0, -2), (7, 7), (None, None)])
def test_ep_log(ep, out):
    got = A._ep_log(ep)
    assert got == out and type(got) is type(out)


_PH = re.compile(r"%(?!%)[-#0 +]*\d*(?:\.\d+)?[sdrfi]")
_EP_SLOT = re.compile(r"第 ?%s ?集")


def test_anime_logs_never_pass_a_raw_float_episode():
    """core/anime.py 里所有 log.* 的『第%s集』占位：对应实参不许是裸的 episode 变量/属性
    （库里是 float，会打成 6.0）——要经 _ep_log / int / ep_ranges 之类的格式化。"""
    tree = ast.parse((_ROOT / "core" / "anime.py").read_text(encoding="utf-8"))
    bad, checked = [], 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "log"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            continue
        fmt = node.args[0].value
        for m in _EP_SLOT.finditer(fmt):
            idx = len(_PH.findall(fmt[:m.start()]))
            arg = node.args[1 + idx]
            checked += 1
            leaf = arg.attr if isinstance(arg, ast.Attribute) else getattr(arg, "id", "")
            if leaf.endswith("episode"):
                bad.append(f"core/anime.py:{node.lineno} {ast.unparse(arg)}")
    assert checked >= 6, f"只找到 {checked} 处『第%s集』，判据失效了"
    assert not bad, f"日志里集号没去 .0：{bad}"
