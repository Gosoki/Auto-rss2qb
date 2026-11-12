"""(R40) 待识别番的两处断点。

① 后台重试命中后当场按『自动升确认』的同一组条件转追番（_promote_after_retry → _auto_promote）。
   不做的话命中后停在『待确认』，要等同一个 auto 源再来一条新种子才升，周更番第 1 集晚约一周。
   只对 auto 源建的番（confirm_reason == "unmatched"）、只在后台这条路；人工审核源、绑定看着不对、
   绑到只有 1 集的条目、像剧场版、早于开始使用日、已下过、另一条番占着同一个 bgm、一条种子都没有、
   人点『重新识别』（包括在后台这一轮跑到一半时点的）的，都不升。
② 重试次数用完后，新集号入库 → 把次数退回到只剩最后一次（_rearm_enrich_on_new_episode），
   让后台再问一次 bgm。每个集号只触发一次；wrong_season / 已忽略 / 有 bgm / 次数没用完的不动。

全部打桩 enrich.resolve，零网络。
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeAlias, AnimeTorrent
from sources.base import ParsedItem
from sources.parse import candidate_names

BGM = 9002
NOW = datetime.now()


def _info(**kw):
    d = {"bangumi_id": BGM, "display_name": "新番甲", "jp_name": "新番甲JP",
         "air_date": (NOW - timedelta(days=3)).date().isoformat(), "total_episodes": 12,
         "platform": "TV", "quarter": "26D"}
    d.update(kw)
    return d


@pytest.fixture
def hit(monkeypatch):
    """enrich.resolve 打桩：返回给定的 bgm 信息（默认一部正常的本季 TV 番），并记调用次数。"""
    calls = {"n": 0, "info": _info()}

    async def _resolve(names, release_time, episode, info_hash):
        calls["n"] += 1
        return calls["info"]
    monkeypatch.setattr(A.enrich, "resolve", _resolve)
    return calls


def _mk(db, *, title="新番甲TW", reason="unmatched", eps=(1,), statuses=None, **kw):
    """一部到点的『待识别』番（bgm 空、建番 30 天前）+ 若干条种子 + 别名。返回 id。"""
    fields = dict(title=title, season=1, quarter="26D", bangumi_id=None, confirmed=False,
                  rejected=False, confirm_reason=reason, enrich_tries=0,
                  created_at=NOW - timedelta(days=30))
    fields.update(kw)
    with db.get_session() as s:
        a = Anime(**fields)
        s.add(a); s.commit(); s.refresh(a)
        for i, ep in enumerate(eps):
            s.add(AnimeTorrent(info_hash=f"{a.id:02d}{i:02d}".ljust(40, "0"), anime_id=a.id, source="ANi",
                               site="nyaa", anime_title=title, raw_title=f"[ANi] {title} - {ep:02d} [1080P]",
                               season=1, episode=ep, quarter="26D", download_url="http://x/1.torrent",
                               release_time=NOW - timedelta(days=1), priority=100,
                               status=(statuses or {}).get(ep, "pending")))
        s.add(AnimeAlias(title=A.alias_key(title), season=1, anime_id=a.id))
        s.commit()
        return a.id


def _get(db, aid):
    with db.get_session() as s:
        return s.get(Anime, aid)


@pytest.fixture
def base_cfg(cfg):
    cfg(REENRICH_MAX_TRIES=5, REENRICH_RETRY_BASE=1, REENRICH_RETRY_MAX=60,
        ANIME_START_DATE=(NOW - timedelta(days=60)).date().isoformat())
    return cfg


# ---------------- ① 后台重试命中 → 转追番 ----------------

async def test_retry_hit_promotes_unmatched(clean_tables, base_cfg, hit):
    """auto 源建的待识别番，后台重试认出来、条件都满足 → 当场转追番。"""
    aid = _mk(clean_tables)
    assert await A.retry_unmatched() == 1
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM
    assert (a.confirmed, a.rejected) == (True, False), f"应转追番，实际 confirmed={a.confirmed}"


async def test_retry_hit_review_not_promoted(clean_tables, base_cfg, hit):
    """人工审核源建的（review）照策略要人点头，不升。"""
    aid = _mk(clean_tables, reason="review")
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM and a.confirmed is False


async def test_retry_hit_binding_looks_wrong_not_promoted(clean_tables, base_cfg, hit):
    """绑到只有 1 集的条目、种子却有两个正集号 → 绑定看着不对，不升。"""
    hit["info"] = _info(total_episodes=1)
    aid = _mk(clean_tables, eps=(1, 2))
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM and a.confirmed is False


async def test_retry_hit_movie_like_not_promoted(clean_tables, base_cfg, hit):
    """bgm 说是剧场版 → 不升，原因改成『像是剧场版』。"""
    hit["info"] = _info(platform="剧场版", total_episodes=1)
    aid = _mk(clean_tables)
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert a.confirmed is False and a.confirm_reason == "movie"


async def test_retry_hit_before_start_not_promoted(clean_tables, base_cfg, hit):
    """早于开始使用日 → 不升，也不判超期忽略，留在待确认。"""
    hit["info"] = _info(air_date=(NOW - timedelta(days=400)).date().isoformat())
    aid = _mk(clean_tables)
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM and (a.confirmed, a.rejected) == (False, False)


async def test_retry_hit_with_handled_torrent_not_promoted(clean_tables, base_cfg, hit):
    """已经下过一集（人工点下过）→ 不升。"""
    aid = _mk(clean_tables, eps=(1, 2), statuses={1: "sent"})
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM and a.confirmed is False


async def test_retry_hit_duplicate_bgm_not_promoted(clean_tables, base_cfg, hit, monkeypatch):
    """库里另一条番已经占着同一个 bgm → 不升，留在待确认等人核对。"""
    async def _quiet(*a, **k):
        return True
    monkeypatch.setattr(A, "notify_event", _quiet)
    aid = _mk(clean_tables)
    with clean_tables.get_session() as s:
        s.add(Anime(title="另一条", season=1, bangumi_id=BGM, confirmed=True)); s.commit()
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM and a.confirmed is False


async def test_manual_reenrich_not_promoted(clean_tables, base_cfg, hit):
    """人点『重新识别』认出来的 → 回到待确认、不升（界面明说去点确认下载）。"""
    aid = _mk(clean_tables)
    assert await A.manual_enrich(aid)
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM and a.confirmed is False and a.confirm_reason == "unmatched"


async def test_process_item_auto_promote_unchanged(clean_tables, base_cfg, hit):
    """抽函数没改 process_item 那一支：已识别待确认的番，auto 源新种子进来照旧升；review 源的种子不升。"""
    aid = _mk(clean_tables, reason="review", bangumi_id=BGM, air_date=_info()["air_date"],
              total_episodes=12)
    await A.process_item(_item(2, policy="review"))
    assert _get(clean_tables, aid).confirmed is False
    await A.process_item(_item(3, policy="auto"))
    assert _get(clean_tables, aid).confirmed is True


# ---------------- ② 次数用完后新集号再试一次 ----------------

def _item(ep, *, source="ANi", policy="auto"):
    return ParsedItem(info_hash=f"{source}{ep:03d}".encode().hex().ljust(40, "0")[:40],
                      raw_title=f"[{source}] 新番甲TW - {ep:02d} [1080P]", anime_title="新番甲TW",
                      season=1, episode=ep, quarter="26D", release_time=NOW, download_url="http://x/2.torrent",
                      source=source, site="nyaa", policy=policy, priority=100,
                      search_names=candidate_names("新番甲TW"))


def _tries(db, aid):
    a = _get(db, aid)
    return a.enrich_tries, a.last_enrich_at


async def test_new_episode_rearms_exhausted_retry(clean_tables, base_cfg, hit):
    """次数用完 + 新集号入库 → 次数退到上限减一、上次尝试时间清空；后台下一轮真的再问一次 bgm。"""
    aid = _mk(clean_tables, enrich_tries=5, last_enrich_at=NOW)
    hit["info"] = None
    await A.process_item(_item(2))
    assert _tries(clean_tables, aid) == (4, None)
    assert hit["n"] == 0, "别名命中，入库本身不问 bgm"
    await A.retry_unmatched()
    assert hit["n"] == 1, "复位后后台应当再问一次"


async def test_same_episode_second_source_does_not_rearm(clean_tables, base_cfg, hit):
    """同一集号第二个源到来不再触发（每个新集号最多一次）。"""
    aid = _mk(clean_tables, enrich_tries=5, last_enrich_at=NOW)
    await A.process_item(_item(2, source="ANi"))
    assert _tries(clean_tables, aid)[0] == 4
    with clean_tables.get_session() as s:      # 后台那一次也没认出来，又用完了
        a = s.get(Anime, aid); a.enrich_tries = 5; a.last_enrich_at = NOW; s.add(a); s.commit()
    await A.process_item(_item(2, source="LoliHouse"))
    t, last = _tries(clean_tables, aid)
    assert t == 5 and last is not None


@pytest.mark.parametrize("kw", [
    {"confirm_reason": "wrong_season"},
    {"rejected": True},
    {"bangumi_id": BGM},
], ids=["wrong_season", "rejected", "has_bgm"])
async def test_rearm_skips(clean_tables, base_cfg, hit, kw):
    """wrong_season（有意打满、别绑回错条目）/ 已忽略 / 已有 bgm：新集号来了也不复位。"""
    aid = _mk(clean_tables, enrich_tries=5, last_enrich_at=NOW, **kw)
    await A.process_item(_item(2))
    t, last = _tries(clean_tables, aid)
    assert t == 5 and last is not None


async def test_rearm_skips_when_tries_left(clean_tables, base_cfg, hit):
    """次数还没用完的不动（退避阶梯照常走）。"""
    aid = _mk(clean_tables, enrich_tries=2, last_enrich_at=NOW)
    await A.process_item(_item(2))
    t, last = _tries(clean_tables, aid)
    assert t == 2 and last is not None


# ---------------- 审查后补的守卫 ----------------

@pytest.mark.parametrize("reason", [None, "backfill", "bind", "released", "wrong_season", "movie"])
async def test_only_unmatched_reason_promotes(clean_tables, base_cfg, hit, reason):
    """只有 unmatched 升：原因为空（这一列上线前的老数据）或别的键都不升——是白名单，不是"除了 review"。"""
    aid = _mk(clean_tables, reason=reason)
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM and a.confirmed is False


async def test_single_episode_entry_first_week_not_promoted(clean_tables, base_cfg, hit):
    """库里只有第 1 集时被认成只有 1 集、类型不是剧场版的条目（同名特别篇 / 先行版 / OVA）：
    binding_looks_wrong 要第二个正集号才看得出来，这一刻验不了 → 留在待确认。"""
    hit["info"] = _info(total_episodes=1, platform="OVA")
    aid = _mk(clean_tables, eps=(1,))
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM and a.confirmed is False and a.confirm_reason == "unmatched"


async def test_no_torrent_anime_not_promoted(clean_tables, base_cfg, hit):
    """一条种子都没有的待识别番：没有任何证据可校验，认出来也不升。"""
    aid = _mk(clean_tables, eps=())
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM and a.confirmed is False


async def test_duplicate_bgm_held_by_rejected_anime_still_blocks(clean_tables, base_cfg, hit, monkeypatch):
    """占着同一个 bgm 的另一条是『已忽略』的番 → 同样不升。"""
    async def _quiet(*a, **k):
        return True
    monkeypatch.setattr(A, "notify_event", _quiet)
    aid = _mk(clean_tables)
    with clean_tables.get_session() as s:
        s.add(Anime(title="忽略过的", season=1, bangumi_id=BGM, confirmed=True, rejected=True)); s.commit()
    await A.retry_unmatched()
    assert _get(clean_tables, aid).confirmed is False


async def test_manual_reenrich_while_queued_in_round(clean_tables, base_cfg, monkeypatch):
    """到点名单是轮首定的：后台在问番一时，用户给排在后面的番二点了『重新识别』并认了出来。
    轮到番二时它已不在池子里，后台不该再识别一遍、更不该把它升成『订阅中』。"""
    _mk(clean_tables, title="番一")
    a2 = _mk(clean_tables, title="番二")
    seq = {"n": 0}

    async def _resolve(names, release_time, episode, info_hash):
        seq["n"] += 1
        if seq["n"] == 1:                       # 后台正在问番一
            assert await A.manual_enrich(a2)    # 用户点番二的『重新识别』（第 2 次调用）
            return None
        return _info()
    monkeypatch.setattr(A.enrich, "resolve", _resolve)
    await A.retry_unmatched()
    b = _get(clean_tables, a2)
    assert b.bangumi_id == BGM and b.confirmed is False
    assert seq["n"] == 2, "番二已有 bgm，后台不该再问一次"


async def test_user_bind_while_queued_in_round_kept(clean_tables, base_cfg, monkeypatch):
    """排队期间用户给番二手动绑定了 bgm：轮到它时后台不许按名字重识别把绑定盖掉。"""
    _mk(clean_tables, title="番一")
    a2 = _mk(clean_tables, title="番二")

    async def _fetch(bgm_id):
        return _info(bangumi_id=bgm_id, display_name="用户绑的")
    monkeypatch.setattr(A.enrich, "fetch_by_id", _fetch)
    seq = {"n": 0}

    async def _resolve(names, release_time, episode, info_hash):
        seq["n"] += 1
        if seq["n"] == 1:
            assert await A.bind_anime_bgm(a2, 777)
            return None
        return _info()                          # 后台按名字搜到的是 9002
    monkeypatch.setattr(A.enrich, "resolve", _resolve)
    await A.retry_unmatched()
    b = _get(clean_tables, a2)
    assert (b.bangumi_id, b.confirmed, b.confirm_reason) == (777, False, "bind")


async def test_manual_reenrich_lands_first_background_missed(clean_tables, base_cfg, monkeypatch):
    """后台那次自己没搜到，人点的『重新识别』在同一个 await 窗口里先落了 bgm：
    enrich_anime 让路、仍返回真，但这不是后台命中，不升。"""
    aid = _mk(clean_tables)
    seq = {"n": 0}

    async def _resolve(names, release_time, episode, info_hash):
        seq["n"] += 1
        if seq["n"] == 1:                       # 后台那次：窗口里用户点了『重新识别』
            assert await A.manual_enrich(aid)
            return None
        return _info()                          # 人点的那次搜到了
    monkeypatch.setattr(A.enrich, "resolve", _resolve)
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert a.bangumi_id == BGM and a.confirmed is False


async def test_refund_skips_rows_left_the_pool(clean_tables, base_cfg, monkeypatch):
    """bgm 整轮不可达要退次数时，只退本轮真正加过一的：轮内被人绑走、跳过的那部不许被减一。"""
    ids = [_mk(clean_tables, title=f"番{i}", enrich_tries=2) for i in range(4)]
    fails = {"n": 0}
    monkeypatch.setattr(A.enrich, "net_failures", lambda: fails["n"])

    async def _fetch(bgm_id):
        return _info(bangumi_id=bgm_id)
    monkeypatch.setattr(A.enrich, "fetch_by_id", _fetch)
    seq = {"n": 0}

    async def _resolve(names, release_time, episode, info_hash):
        seq["n"] += 1
        fails["n"] += 1                         # 每一部都是连接层失败
        if seq["n"] == 1:
            assert await A.bind_anime_bgm(ids[3], 777)
        return None
    monkeypatch.setattr(A.enrich, "resolve", _resolve)
    await A.retry_unmatched()
    assert [_get(clean_tables, i).enrich_tries for i in ids] == [2, 2, 2, 2]


async def test_process_item_does_not_touch_overdue_ignored(clean_tables, base_cfg, hit):
    """超期忽略 (rejected=1, confirmed=0) 的番，auto 源新种子进来不许被改成 (1,1)。"""
    aid = _mk(clean_tables, reason=None, bangumi_id=BGM, rejected=True, air_date=_info()["air_date"],
              total_episodes=12)
    await A.process_item(_item(2))
    a = _get(clean_tables, aid)
    assert (a.confirmed, a.rejected) == (False, True)


async def test_process_item_confirmed_movie_like_reason_untouched(clean_tables, base_cfg, hit):
    """已在追的番（人确认过）像剧场版：auto 源新种子进来不许把原因改成 movie
    （deliver_single_works 把 movie 当"人看过了"的记号）。"""
    aid = _mk(clean_tables, reason=None, bangumi_id=BGM, confirmed=True, platform="剧场版",
              total_episodes=1, air_date=_info()["air_date"])
    await A.process_item(_item(1, source="Other"))
    assert _get(clean_tables, aid).confirm_reason is None


async def test_rearm_ignores_other_animes_same_episode(clean_tables, base_cfg, hit):
    """"这一集已有"只看本番：别的番也有第 2 集，照样复位。"""
    _mk(clean_tables, title="别的番", eps=(2,), bangumi_id=777)
    aid = _mk(clean_tables, enrich_tries=5, last_enrich_at=NOW)
    await A.process_item(_item(2))
    assert _tries(clean_tables, aid) == (4, None)


@pytest.mark.parametrize("reason", [None, "review"])
async def test_rearm_for_non_unmatched_reasons(clean_tables, base_cfg, hit, reason):
    """只排除 wrong_season：人工审核源建的 / 原因为空的待识别番，次数用完后新集号也复位。"""
    aid = _mk(clean_tables, reason=reason, enrich_tries=5, last_enrich_at=NOW)
    await A.process_item(_item(2))
    assert _get(clean_tables, aid).enrich_tries == 4


async def test_rearm_boundary_one_try_left_untouched(clean_tables, base_cfg, hit):
    """还剩最后一次（上限减一）的不动：上次尝试时间不许被清掉（否则退避被跳过）。"""
    aid = _mk(clean_tables, enrich_tries=4, last_enrich_at=NOW)
    await A.process_item(_item(2))
    t, last = _tries(clean_tables, aid)
    assert t == 4 and last is not None


async def test_rearm_by_review_source_torrent(clean_tables, base_cfg, hit):
    """复位不看这条种子的源策略：auto 源建的番次数用完，人工审核源带来新集号 → 复位 → 后台命中 → 升。
    （升不升看的是建番时的来源 confirm_reason，不是这条种子。）"""
    aid = _mk(clean_tables, enrich_tries=5, last_enrich_at=NOW)
    await A.process_item(_item(2, source="Rev", policy="review"))
    assert _get(clean_tables, aid).enrich_tries == 4
    await A.retry_unmatched()
    assert _get(clean_tables, aid).confirmed is True


async def test_rearm_review_source_anime_then_hit_not_promoted(clean_tables, base_cfg, hit):
    """人工审核源建的待识别番：新集照样复位再问，命中后仍不升。"""
    aid = _mk(clean_tables, reason="review", enrich_tries=5, last_enrich_at=NOW)
    await A.process_item(_item(2, policy="review"))
    await A.retry_unmatched()
    a = _get(clean_tables, aid)
    assert hit["n"] == 1 and a.bangumi_id == BGM and a.confirmed is False


@pytest.mark.parametrize("ep,want", [(2, "第2集"), (-2, "未知集")])
async def test_rearm_log_wording(clean_tables, base_cfg, hit, caplog, ep, want):
    """复位日志的集号用推送同一套词，不打出"第 -2.0 集"。"""
    import logging
    caplog.set_level(logging.INFO)
    _mk(clean_tables, enrich_tries=5, last_enrich_at=NOW)
    it = _item(2)
    it.episode = ep
    await A.process_item(it)
    msgs = [r.getMessage() for r in caplog.records if "再排一次 bgm 识别" in r.getMessage()]
    assert msgs and f"来了新的{want}" in msgs[0], msgs
