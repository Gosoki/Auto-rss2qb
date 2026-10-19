"""缺集提示：按开播日推算应已播出、库里却一条种子都没见过的集。

两个真实案例（2026-10，230 库）：
  · 『FX战士久留美』采集停了半个月，ANi 第 1 集滚出了 RSS 窗口，卡片却显示 2/2、蓝色；
  · 『尼古喵喵』锁定 ANi，而 ANi 发到第 7 集就断更了，卡片显示 7/7、蓝色，实际缺 8~12。
"""
from datetime import date, datetime

from core import anime as A
from db.models import Anime, AnimeTorrent
from pages.layout import ep_ranges_text

TODAY = date(2026, 10, 19)


def _a(**kw):
    base = dict(title="x", season=1, quarter="26D", confirmed=True, rejected=False,
                total_episodes=12, air_date="2026-07-02")
    base.update(kw)
    return Anime(**base)


def test_locked_source_stopped_midway(cfg):
    """尼古喵喵：7 月 2 日开播、12 集，库里只有 1~7 —— 缺 8~12。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2)
    assert A.missing_episodes(_a(), set(range(1, 8)), TODAY) == [8, 9, 10, 11, 12]


def test_first_episode_scrolled_out_of_rss(cfg):
    """FX：10 月 1 日开播，库里只有 2、3 —— 缺第 1 集（中间/开头的缺口也要抓到）。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2)
    a = _a(total_episodes=14, air_date="2026-10-01")
    assert A.missing_episodes(a, {2, 3}, TODAY) == [1]


def test_caught_up_show_has_no_gap(cfg):
    cfg(ANIME_GAP_SLACK_WEEKS=2)
    a = _a(total_episodes=14, air_date="2026-10-01")
    assert A.missing_episodes(a, {1, 2, 3}, TODAY) == []


def test_slack_tolerates_a_late_week(cfg):
    """余量 2 周：最新一集晚一周到不算缺（休播/字幕组晚发）；余量调成 0 就会报出来。"""
    a = _a(total_episodes=14, air_date="2026-10-01")      # 到 10/19 按周更应播到第 3 集
    cfg(ANIME_GAP_SLACK_WEEKS=2)
    assert A.missing_episodes(a, {1, 2}, TODAY) == []
    cfg(ANIME_GAP_SLACK_WEEKS=0)
    assert A.missing_episodes(a, {1, 2}, TODAY) == [3]


def test_absolute_numbering_is_not_reported(cfg):
    """第二部分用绝对编号写 13、14：『缺第 1 集』是假的，不报。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2)
    a = _a(total_episodes=15, air_date="2026-10-05")
    assert A.missing_episodes(a, {13, 14}, TODAY) == []


def test_episode_beyond_total_is_not_reported(cfg):
    """有超出总集数的集号（别的季的正片挂进来，已有红色徽标）—— 集号对不上本季，不报。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2)
    assert A.missing_episodes(_a(), {1, 2, 13}, TODAY) == []


def test_only_tracking_shows_are_judged(cfg):
    """待确认/已忽略的番本来就不下，报缺集是噪音。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2)
    seen = set(range(1, 8))
    assert A.missing_episodes(_a(confirmed=False), seen, TODAY) == []
    assert A.missing_episodes(_a(rejected=True), seen, TODAY) == []


def test_no_regular_episode_at_all_is_not_judged(cfg):
    """库里只有特别篇/没集号的种子（缎带英雄这类单部作品）：不能说它『缺第 1 集』。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2)
    assert A.missing_episodes(_a(total_episodes=1, platform="WEB"), set(), TODAY) == []


def test_unknown_total_or_air_date_is_not_judged(cfg):
    cfg(ANIME_GAP_SLACK_WEEKS=2)
    seen = {1}
    assert A.missing_episodes(_a(total_episodes=None), seen, TODAY) == []
    assert A.missing_episodes(_a(air_date=None), seen, TODAY) == []


def test_ep_ranges_text():
    assert ep_ranges_text([1, 8, 9, 10, 11, 12]) == "1、8～12"
    assert ep_ranges_text([3]) == "3"
    assert ep_ranges_text([]) == ""


def test_episode_gaps_reads_the_library(clean_tables, cfg):
    """接库：只返回有缺口的番；口径与列表上的比值同一份（_episode_sets）。"""
    cfg(ANIME_GAP_SLACK_WEEKS=2)
    now = datetime.now()
    with clean_tables.get_session() as s:
        gap = _a(title="缺集番", air_date="2025-01-05")           # 早就播完了
        full = _a(title="齐全番", air_date="2025-01-05")
        s.add(gap); s.add(full); s.commit(); s.refresh(gap); s.refresh(full)
        n = 0
        for aid, eps in ((gap.id, range(1, 8)), (full.id, range(1, 13))):
            for ep in eps:
                n += 1
                s.add(AnimeTorrent(anime_id=aid, info_hash=f"{n:040x}", raw_title=f"t{n}",
                                   episode=ep, status="sent", created_at=now))
        s.commit()
        gap_id, full_id = gap.id, full.id
        animes = [s.get(Anime, gap_id), s.get(Anime, full_id)]
    got = A.episode_gaps(animes)
    assert got == {gap_id: [8, 9, 10, 11, 12]}, got
