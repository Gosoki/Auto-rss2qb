"""r38 全面审计之后的修复（docs/audit-2026-10-r38.md）。"""
import asyncio


def test_wait_dialog_returns_none_when_page_is_closed():
    """确认框开着时用户关掉/刷新了页面：NiceGUI 的 `await dialog` 永远不返回，处理器挂住、busy_action 的
    去重键一直占着（切库按钮就此全部失灵，只能重启）。wait_dialog 要在页面被删时当作取消返回 None。"""
    from pages.layout import wait_dialog

    class _Client:
        def __init__(self):
            self.handlers = []

        def on_delete(self, h):
            self.handlers.append(h)

    class _Dlg:
        def __init__(self):
            self.client = _Client()
            self._ev = asyncio.Event()
            self._result = "unset"

        def submit(self, v):
            self._result = v
            self._ev.set()

        def __await__(self):
            yield from self._ev.wait().__await__()
            return self._result

    async def run():
        d = _Dlg()
        task = asyncio.ensure_future(wait_dialog(d))
        await asyncio.sleep(0)
        assert not task.done()
        for h in d.client.handlers:      # 页面被删
            h()
        return await asyncio.wait_for(task, 1)

    assert asyncio.run(run()) is None


# ── 补齐：挡前几季的同号集、自动补齐遵守锁定源 ───────────────────────

def _bf_setup(clean_tables, cfg, monkeypatch, found, *, pref=None, air="2026-07-05", rows=()):
    import sources
    from datetime import datetime
    from core import anime as A
    from db.models import Anime, AnimeTorrent

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        async def fetch(self, content_cache=None):
            return list(found)

    for site in ("nyaa", "mikan"):
        monkeypatch.setitem(sources.SOURCES, site, _Stub)
        monkeypatch.setitem(sources.SEARCH_URL, site, lambda q: f"https://x/?q={q}")
    sent = []

    async def _fake_dl(tid, force=False, **kw):   # **kw：补齐下发传 prefs_gate（r39 D4）
        sent.append(tid)
        return True

    async def _alive():
        return True
    monkeypatch.setattr(A, "download_anime_torrent", _fake_dl)
    monkeypatch.setattr(A, "qb_precheck", _alive)
    cfg(ANIME_BACKFILL_PREAIR_DAYS=30, ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    with clean_tables.get_session() as s:
        a = Anime(title="100个女朋友", display_name="100个女朋友 第三季", season=3, quarter="26C",
                  confirmed=True, rejected=False, total_episodes=12, air_date=air, bangumi_id=1, pref_source=pref)
        s.add(a); s.commit(); s.refresh(a)
        for i, (src, ep) in enumerate(rows):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{9000 + i:040x}", source=src, site="nyaa",
                               anime_title="100个女朋友", season=1, episode=ep, status="sent",
                               raw_title=f"[{src}] 100个女朋友 - {ep:02d}", created_at=datetime.now()))
        s.commit()
        return a.id, sent


def _bf_item(group, ep, n, released):
    from sources.base import ParsedItem
    return ParsedItem(info_hash=f"{n:040x}", raw_title=f"[{group}] 100个女朋友 - {ep:02d} [1080p]",
                      anime_title="100个女朋友", season=1, episode=float(ep), quarter="26C",
                      release_time=released, download_url=f"https://x/{n}.torrent", source=group,
                      site="nyaa", search_names=["100个女朋友"])


def test_backfill_skips_previous_seasons_same_numbers(clean_tables, cfg, monkeypatch):
    """ANi 各季同一个标题、不写第几季：补齐搜回来的 2023 年第一季『- 05』不能当成第三季第 5 集收进来（真库 #6）。
    本季开播之后的照收。"""
    import asyncio
    from datetime import datetime
    from core import anime as A
    found = [_bf_item("ANi", 5, 1, datetime(2023, 11, 5)),        # 第一季：早两年多（原有判据 ③ 就能挡）
             _bf_item("ANi", 6, 2, datetime(2026, 5, 3)),         # 上一部分（分割放送）：早两个月，③ 挡不住
             _bf_item("ANi", 30, 3, datetime(2026, 8, 9))]        # 本季（绝对编号）
    aid, sent = _bf_setup(clean_tables, cfg, monkeypatch, found, rows=[("ANi", 29)])
    res = asyncio.run(A.backfill_source(aid, name_filter=True))
    assert (res["ingested"], res["too_old"]) == (1, 2), res
    cfg(ANIME_BACKFILL_PREAIR_DAYS=0)
    res = asyncio.run(A.backfill_source(aid, name_filter=True))
    assert (res["ingested"], res["too_old"]) == (1, 1), "0=只剩原有判据：早两个月的收、早两年的照样挡"


def test_auto_backfill_downloads_only_the_locked_group(clean_tables, cfg, monkeypatch):
    """锁了『喵萌奶茶屋』：自动补齐按子串把『喵萌奶茶屋&LoliHouse』也收进来，但直接下载时只下锁定组那份
    （后台放行是精确相等，联合发布那份先下就会把锁定组真正的那一集永远挡在外面）。"""
    import asyncio
    from datetime import datetime
    from core import anime as A
    from db.models import AnimeTorrent
    from sqlmodel import select
    found = [_bf_item("喵萌奶茶屋&LoliHouse", 8, 1, datetime(2026, 8, 30)),
             _bf_item("喵萌奶茶屋", 9, 2, datetime(2026, 9, 6))]
    aid, sent = _bf_setup(clean_tables, cfg, monkeypatch, found, pref="喵萌奶茶屋", rows=[("喵萌奶茶屋", 7)])
    res = asyncio.run(A.backfill_source(aid, name_filter=True))
    with clean_tables.get_session() as s:
        by_src = {t.id: t.source for t in s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid))}
    assert [by_src[i] for i in sent] == ["喵萌奶茶屋"], (res, sent)


# ── 锁定源：中间跳过的集、一贯晚几天的终集 ─────────────────────────────

def _lock_setup(clean_tables, rows, lock="喵萌奶茶屋"):
    from datetime import datetime, timedelta
    from db.models import Anime, AnimeTorrent
    base = datetime.now() - timedelta(days=120)
    with clean_tables.get_session() as s:
        a = Anime(title="相反", display_name="相反", season=1, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=12, air_date="2026-07-05", bangumi_id=1, pref_source=lock)
        s.add(a); s.commit(); s.refresh(a)
        for i, (src, ep, day, st) in enumerate(rows):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i + 1:040x}", source=src, site="nyaa",
                               raw_title=f"[{src}] 相反 - {ep:02d}", episode=ep, status=st,
                               created_at=base + timedelta(days=day)))
        s.commit()
        s.refresh(a)
        s.expunge(a)
        return a


def test_lock_group_skipping_a_middle_episode_is_reported(clean_tables, cfg):
    """锁喵萌：它发了 1~5、7~12，第 6 集一直没有，ANi 有。以前只看『锁定组最大集号之后』，第 6 集永远不报、
    仪表盘还把它算成备用项。四处口径要一致：提醒、仪表盘分档、详情页标注、『只补这几集』。"""
    from core import anime as A
    cfg(ANIME_LOCK_LAG_DAYS=7, ANIME_FINISH_UNSUB=False)
    rows = [("ANi", e, 7 * e, "pending") for e in range(1, 13)]
    rows += [("喵萌奶茶屋", e, 7 * e + 1, "sent") for e in range(1, 13) if e != 6]
    a = _lock_setup(clean_tables, rows)
    assert A.locked_out_episodes([a])[a.id]["eps"] == [6]
    assert list(A.locked_out_candidates(a.id)) == [6.0]
    assert A.pending_breakdown()["locked"] == 1
    with clean_tables.get_session() as s:
        from db.models import AnimeTorrent
        from sqlmodel import select
        ts = list(s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == a.id)))
    assert {t.episode for t in ts if t.id in A.locked_out_torrent_ids(ts)} == {6.0}


def test_two_numberings_in_parallel_are_not_a_lag(clean_tables, cfg):
    """锁定组季内 1~12、ANi 绝对 13~24 同步在发：不算落后（±2 天内锁定组发的集号与 k 差得远）。"""
    from core import anime as A
    cfg(ANIME_LOCK_LAG_DAYS=7)
    rows = [("ANi", 12 + e, 7 * e, "pending") for e in range(1, 13)]
    rows += [("喵萌奶茶屋", e, 7 * e + 1, "sent") for e in range(1, 13)]
    a = _lock_setup(clean_tables, rows)
    assert A.locked_out_episodes([a]) == {}


def test_lock_group_one_week_late_still_reports_the_last_episode(clean_tables, cfg):
    """锁定组一贯晚 6 天：它的第 11 集与别组第 12 集前后脚入库。以前被当成"两套编号同步"豁免掉，
    终集永远不报；同一套编号（集号只差 1）不再豁免。"""
    from core import anime as A
    cfg(ANIME_LOCK_LAG_DAYS=7)
    rows = [("ANi", e, 7 * e, "pending") for e in range(1, 13)]
    rows += [("喵萌奶茶屋", e, 7 * e + 6, "sent") for e in range(1, 12)]
    a = _lock_setup(clean_tables, rows)
    assert A.locked_out_episodes([a])[a.id]["eps"] == [12]


def test_bgm_undercount_does_not_hide_other_gaps(cfg):
    """bgm 记 12 集、实际 13 集：第 13 集进库后，前面缺的第 5 集照样要报（以前一律"超出总集数不判"）。
    超出的有 3 个以上才当两套编号、不判。"""
    from datetime import date
    from core import anime as A
    from db.models import Anime
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    a = Anime(title="x", season=1, quarter="26C", confirmed=True, rejected=False, total_episodes=12,
              air_date="2026-07-02")
    today = date(2026, 10, 20)
    seen = set(range(1, 14)) - {5}
    assert A.missing_episodes(a, seen, today) == [5]
    assert A.missing_episodes(a, set(range(1, 13)) | {13, 14, 15}, today) == [], "超出 3 个：两套编号，不判"


def test_ignored_airing_fact_changes_when_it_becomes_misrouted():
    """普通的"跨季连播"被点过知道了；之后别的季的集被分到这条（impossible），提醒要重新出现。
    普通的那种指纹与以前一致（升级后已读的不会重新冒出来）。"""
    from core import alerts
    from datetime import datetime
    g = {"id": 100, "born": datetime(2026, 7, 26), "name": "Re", "bgm": 140001, "confirmed": False, "rejected": True,
         "eps": [84], "total": 26, "impossible": False, "sibling": None}
    plain = alerts._ign(g)["fact"]
    assert plain == "bgm=140001;st=01"
    misrouted = alerts._ign({**g, "impossible": True, "sibling": "Re 第四季"})["fact"]
    assert misrouted != plain and alerts.fact_diff(plain, misrouted)


def test_excluded_only_episode_counts_as_missing(clean_tables, cfg):
    """第 5 集唯一的一份被人工排除（内容不对）：这一集既不会下，也该报缺。以前排除的也算"见过"，一声不吭。"""
    from datetime import date
    from core import anime as A
    from db.models import Anime, AnimeTorrent
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100)
    with clean_tables.get_session() as s:
        a = Anime(title="x", season=1, quarter="26C", confirmed=True, rejected=False, total_episodes=12,
                  air_date="2026-07-02", bangumi_id=1)
        s.add(a); s.commit(); s.refresh(a)
        for e in range(1, 9):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{e:040x}", raw_title=f"x - {e}", episode=e,
                               status="excluded" if e == 5 else "sent"))
        s.commit()
        s.refresh(a); s.expunge(a)
    seen = A._episode_sets([a.id])[a.id][1]
    assert 5 not in seen
    assert 5 in A.missing_episodes(a, seen, date(2026, 9, 10))


def test_deleting_all_source_groups_does_not_reseed(clean_tables):
    """用户在订阅源页把源组删光（不想采了）：下次启动不能又把 ANi（自动下载）种回来。"""
    from sqlmodel import delete, select
    from core import anime as A
    from db import get_meta_session
    from db.models import Setting, SourceGroup
    with get_meta_session() as s:          # 本用例从"全新库"开始：清掉别的用例落下的闩
        for k in list(s.exec(select(Setting).where(Setting.key.like("_SOURCES_SEEDED%")))):
            s.delete(k)
        s.commit()
    with clean_tables.get_session() as s:
        s.exec(delete(SourceGroup))
        s.commit()
    A.seed_source_groups()
    with clean_tables.get_session() as s:
        assert s.exec(select(SourceGroup)).first() is not None, "全新库首启照样种入"
        s.exec(delete(SourceGroup))
        s.commit()
    A.seed_source_groups()
    with clean_tables.get_session() as s:
        assert s.exec(select(SourceGroup)).first() is None, "删光之后不再种回来"
    with get_meta_session() as s:          # 别把闩留给后面依赖首启种入的用例
        for k in list(s.exec(select(Setting).where(Setting.key.like("_SOURCES_SEEDED%")))):
            s.delete(k)
        s.commit()


def test_deleting_files_that_qb_no_longer_has_is_reported_honestly(clean_tables, monkeypatch):
    """qB 对不认识的 hash 也回成功：种子早被人在 qB 里手动移除（生产上 713 条）时，删除只能标记已删，
    文件没动 —— 页面要能说实话。整部删、单集删都一样。"""
    import asyncio
    from core import anime as A, engine
    from db.models import Anime, AnimeTorrent
    with clean_tables.get_session() as s:
        a = Anime(title="x", season=1, quarter="26C", confirmed=True, rejected=True, total_episodes=12)
        s.add(a); s.commit(); s.refresh(a)
        for e, h in ((1, "a" * 40), (2, "b" * 40)):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=h, raw_title=f"x - {e}", episode=e, status="sent",
                               save_path="E:/A/x"))
        s.commit()
        aid = a.id
    deleted = []

    async def info(hs):
        return {"a" * 40: {"state": "stoppedUP"}}      # 只有第 1 集还在 qB 里

    async def delete(hs, delete_files=True):
        deleted.append((tuple(hs), delete_files))
        return True
    monkeypatch.setattr(engine.qb, "torrents_info", info)
    monkeypatch.setattr(engine.qb, "delete", delete)
    r = asyncio.run(A.delete_anime_files(aid))
    assert (r["total"], r["deleted"], r["left"], r["paths"]) == (2, 1, 1, ["E:/A/x"])
    assert deleted == [(("a" * 40,), True)], "不在 qB 里的那条不去删（删了也是假成功）"


def test_single_work_without_episode_number_gets_one_copy(clean_tables, cfg, monkeypatch):
    """缎带英雄（WEB、bgm 只有 1 集）：3 个版本都没标集号（-2），以前一份都不会下。在追的单部作品挑一份下：
    同组同优先级时挑 v2；人工删过一份的不再下；TV 番、人工『设为特别篇』的不动。"""
    import asyncio
    from datetime import datetime, timedelta
    from core import anime as A
    from db.models import Anime, AnimeTorrent
    cfg(ANIME_SINGLE_WORK_AUTO=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_FINISH_UNSUB=False)
    old = datetime.now() - timedelta(hours=3)
    with clean_tables.get_session() as s:
        work = Anime(title="缎带英雄", season=1, quarter="26C", confirmed=True, rejected=False,
                     total_episodes=1, platform="WEB", bangumi_id=1)
        done = Anime(title="删过的片", season=1, quarter="26C", confirmed=True, rejected=False,
                     total_episodes=1, platform="WEB", bangumi_id=2)
        tv = Anime(title="TV 番", season=1, quarter="26C", confirmed=True, rejected=False,
                   total_episodes=1, platform="TV", bangumi_id=3)
        for a in (work, done, tv):
            s.add(a)
        s.commit()
        rows = [(work.id, "Nix-Raws", 10, "[Nix-Raws] THE RIBBON HERO 2026 [NF WEB-DL 1080p]", -2, "pending"),
                (work.id, "LoliHouse", 40, "[SweetSub&LoliHouse] 缎带英雄 [WebRip 1080p]", -2, "pending"),
                (work.id, "LoliHouse", 40, "[SweetSub&LoliHouse] 缎带英雄 [v2][WebRip 1080p]", -2, "pending"),
                (work.id, "LoliHouse", 40, "[SweetSub&LoliHouse] 缎带英雄 特典 [WebRip]", -1, "pending"),
                (done.id, "X", 40, "[X] 删过的片 [1080p]", -2, "deleted"),
                (done.id, "X", 40, "[X] 删过的片 [720p]", -2, "pending"),
                (tv.id, "X", 40, "[X] TV 番 [1080p]", -2, "pending")]
        ids = []
        for i, (aid, src, pri, raw, ep, st) in enumerate(rows):
            # (r39 E-73) "删过的片"删的是它仅剩的一份 → 该集已删（ep_dropped）；只删重复份的另见 test_r39_deleted
            t = AnimeTorrent(anime_id=aid, info_hash=f"{i + 1:040x}", source=src, priority=pri, raw_title=raw,
                             episode=ep, status=st, created_at=old, ep_dropped=(st == "deleted"))
            s.add(t)
            s.commit()
            ids.append(t.id)
    sent = []

    async def fake_dl(tid, force=False):
        sent.append(tid)
        return True
    monkeypatch.setattr(A, "download_anime_torrent", fake_dl)
    assert asyncio.run(A.deliver_single_works()) == 1
    assert sent == [ids[2]], "挑优先级最高的组里的 v2"
    cfg(ANIME_SINGLE_WORK_AUTO=False)
    sent.clear()
    assert asyncio.run(A.deliver_single_works()) == 0 and sent == []


def test_movie_like_single_works_go_back_to_pending_confirm(clean_tables, cfg, monkeypatch):
    """剧场版逐版本人工点下：混进番剧表、在追、只有 1 集的剧场版（类型是剧场版 / bgm 剧场版表里也有 /
    种子剧场版表也收了）不自动下，打回『待确认·像是剧场版』等人决定 —— 不是静默跳过。
    用户在待确认上点了确认（confirm_anime 不清原因），下一轮照常挑一份下；
    剧场版那边已经下过的同一个种子不再从这边下。WEB 单部作品照常下。"""
    import asyncio
    from datetime import datetime, timedelta
    from core import anime as A
    from db.models import Anime, AnimeTorrent, Movie, MovieTorrent
    cfg(ANIME_SINGLE_WORK_AUTO=True, QB_ENABLED=True, ANIME_DOWNLOAD_GRACE_MIN=0, ANIME_FINISH_UNSUB=False)
    old = datetime.now() - timedelta(hours=3)
    with clean_tables.get_session() as s:
        film = Anime(title="剧场版 甲", season=1, quarter="26C", confirmed=True, rejected=False,
                     total_episodes=1, platform="剧场版", bangumi_id=11)
        twin = Anime(title="乙", season=1, quarter="26C", confirmed=True, rejected=False,
                     total_episodes=1, platform="WEB", bangumi_id=12)
        shared = Anime(title="丙", season=1, quarter="26C", confirmed=True, rejected=False,
                       total_episodes=1, platform="WEB", bangumi_id=13)
        web = Anime(title="丁", season=1, quarter="26C", confirmed=True, rejected=False,
                    total_episodes=1, platform="WEB", bangumi_id=14)
        for a in (film, twin, shared, web):
            s.add(a)
        s.commit()
        m1 = Movie(title="乙", bangumi_id=12)
        m2 = Movie(title="丙剧场版", bangumi_id=99)
        s.add(m1); s.add(m2); s.commit()
        s.add(MovieTorrent(movie_id=m2.id, info_hash=f"{3:040x}", raw_title="[X] 丙", status="sent"))
        ids, aids = [], [a.id for a in (film, twin, shared, web)]
        for i, a in enumerate((film, twin, shared, web)):
            t = AnimeTorrent(anime_id=a.id, info_hash=f"{i + 1:040x}", source="X", priority=10,
                             raw_title=f"[X] {a.title} [1080p]", episode=-2, status="pending", created_at=old)
            s.add(t)
            s.commit()
            ids.append(t.id)
    sent = []

    async def fake_dl(tid, force=False):
        sent.append(tid)
        with clean_tables.get_session() as s:
            t = s.get(AnimeTorrent, tid)
            t.status = "sent"
            s.add(t)
            s.commit()
        return True
    monkeypatch.setattr(A, "download_anime_torrent", fake_dl)
    assert asyncio.run(A.deliver_single_works()) == 1
    assert sent == [ids[3]], "只下 WEB 的丁"
    with clean_tables.get_session() as s:
        for aid in aids[:3]:
            a = s.get(Anime, aid)
            assert (a.confirmed, a.rejected, a.confirm_reason) == (False, False, "movie"), a.title
        assert s.get(Anime, aids[3]).confirmed
    # 用户在待确认上点了确认：下一轮挑一份下；丙的种子剧场版那边已经下过，不再从这边下
    for aid in aids[:3]:
        A.confirm_anime(aid)
    sent.clear()
    assert asyncio.run(A.deliver_single_works()) == 2
    assert sorted(sent) == sorted([ids[0], ids[1]])
    with clean_tables.get_session() as s:
        assert s.get(Anime, aids[2]).confirmed, "人确认过的不再打回待确认"


async def test_new_movie_from_auto_source_is_not_auto_confirmed(monkeypatch, clean_tables, cfg):
    """自动源新收的番，bgm 说是剧场版：不自动追，进『待确认·像是剧场版』；之后的新种子也不会把它自动升成追番。"""
    from datetime import datetime
    from core import anime as A
    from db.models import Anime, AnimeTorrent
    from sources.base import ParsedItem
    from sqlmodel import select
    cfg(ANIME_START_DATE="")

    async def _fake_resolve(*a_, **k_):
        return {"bangumi_id": 777, "display_name": "某剧场版", "air_date": "2026-09-01",
                "total_episodes": 1, "platform": "剧场版"}
    monkeypatch.setattr(A.enrich, "resolve", _fake_resolve)
    for n in (1, 2):
        item = ParsedItem(info_hash=f"{n:040x}", raw_title=f"[组] 某剧场版 [{n}080p]", anime_title="某剧场版",
                          season=1, episode=-2, quarter="26C", release_time=datetime(2026, 9, 2),
                          download_url="http://x/y.torrent", source="组", site="nyaa", policy="auto")
        item.search_names = ["某剧场版"]
        await A.process_item(item)
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent)).first()
        a = s.get(Anime, t.anime_id)
        assert (a.confirmed, a.rejected, a.confirm_reason) == (False, False, "movie")
        assert A.confirm_reason_text(a.confirm_reason)[0] == "像是剧场版"


def test_gap_is_pushed(clean_tables, cfg, monkeypatch):
    """在追的番有缺集：推一条 gap（以前只挂在仪表盘上）。没订阅这个事件就不推。"""
    import asyncio
    from datetime import date
    from core import anime as A
    from db.models import Anime, AnimeTorrent
    cfg(ANIME_GAP_SLACK_WEEKS=2, ANIME_GAP_MAX_EPISODES=100, NOTIFY_EVENTS=["gap"], NOTIFY_URL="https://x", ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        a = Anime(title="x", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=12, air_date=(date.today().replace(day=1)).isoformat(), bangumi_id=1)
        s.add(a); s.commit(); s.refresh(a)
        aid = a.id
    monkeypatch.setattr(A, "episode_gaps", lambda animes, sets=None: {aid: [3, 4]})
    sent = []

    async def fake(kind, msg, key=None, cooldown=None, **kw):
        sent.append((kind, key, msg))
        return True
    monkeypatch.setattr(A, "notify_event", fake)
    assert asyncio.run(A.sweep_gaps()) == 1
    assert sent[0][0] == "gap" and sent[0][1] == f"{aid}:3,4" and "缺第 3～4 集" in sent[0][2]
    cfg(NOTIFY_EVENTS=["delivered"])
    sent.clear()
    assert asyncio.run(A.sweep_gaps()) == 0 and sent == []


def test_same_number_released_much_later_is_flagged(clean_tables, cfg):
    """真库 #8：已下的第 6~8 集是早先下的（20 周前），绿茶新发的第 6~8 集（季内编号的新一季）撞上同号被挡住、
    一声不吭。晚 20 周以上就提醒；只晚几天（正常的别组版本）不提醒。"""
    from datetime import datetime, timedelta
    from core import alerts, anime as A
    from db.models import Anime, AnimeTorrent
    cfg(ANIME_RERELEASE_WEEKS=20, ANIME_FINISH_UNSUB=False)
    t0 = datetime(2026, 3, 1)
    with clean_tables.get_session() as s:
        a = Anime(title="x", display_name="相反", season=2, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=13, bangumi_id=1)
        s.add(a); s.commit(); s.refresh(a)
        n = 0
        for ep in (6, 7, 8):
            for src, st, rt in (("喵萌", "sent", t0 + timedelta(weeks=ep)),
                                ("绿茶", "pending", t0 + timedelta(weeks=ep + 22)),
                                ("ANi", "skipped", t0 + timedelta(weeks=ep, days=2))):
                n += 1
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{n:040x}", raw_title=f"[{src}] x [{ep:02d}]",
                                   episode=ep, status=st, source=src, release_time=rt))
        s.commit()
    got = A.suspect_rerelease_collision()
    assert [(g["eps"], g["groups"]) for g in got] == [([6, 7, 8], ["绿茶"])]
    assert any(f["kind"] == "rer" and "第 6～8 集" in f["text"] for f in alerts.scan())
    cfg(ANIME_RERELEASE_WEEKS=0)
    assert A.suspect_rerelease_collision() == []


def test_skipped_siblings_come_back_when_the_chosen_one_is_excluded_or_failing(clean_tables, cfg):
    """首选那条被人工排除、或取种失败正在退避重试：被同集去重压成 skipped 的兄弟要放回待下（换个源试）。
    以前只认 error —— 排除了首选，这一集就永远漏掉；取种失败要等约 40 小时退避用完。删过的集不复活。"""
    from core import anime as A
    from db.models import Anime, AnimeTorrent
    cfg(ANIME_FINISH_UNSUB=False)
    with clean_tables.get_session() as s:
        a = Anime(title="x", season=1, quarter="26C", confirmed=True, rejected=False, total_episodes=12, bangumi_id=1)
        s.add(a); s.commit(); s.refresh(a)
        rows = [(1, "excluded", 0), (1, "skipped", 0),          # 第 1 集：首选被排除
                (2, "pending", 2), (2, "skipped", 0),           # 第 2 集：首选取种失败、退避中
                (3, "deleted", 0), (3, "skipped", 0),           # 第 3 集：人工删过 → 不复活
                (4, "pending", 0), (4, "skipped", 0)]           # 第 4 集：首选还没试过 → 不动
        ids = []
        for i, (ep, st, rc) in enumerate(rows):
            # (r39 E-73) 第 3 集删的是最后一份 → 该集已删；只删重复份的不再挡复活，见 test_r39_deleted
            t = AnimeTorrent(anime_id=a.id, info_hash=f"{i + 1:040x}", raw_title=f"x {ep}", episode=ep,
                             status=st, retry_count=rc, source=f"g{i}", ep_dropped=(st == "deleted"))
            s.add(t); s.commit()
            ids.append(t.id)
    A._revive_orphaned_skipped()
    with clean_tables.get_session() as s:
        st = {i: s.get(AnimeTorrent, i).status for i in ids}
    assert [st[ids[k]] for k in (1, 3, 5, 7)] == ["pending", "pending", "skipped", "skipped"]


def test_missing_download_dir_keeps_the_torrent_pending(clean_tables, cfg):
    """下载目录没填：这是配置问题，不是这一条种子的毛病。留在待下（以前记 error，flush 不重试，只能手动补下）。"""
    import asyncio
    from core import anime as A
    from db.models import Anime, AnimeTorrent
    cfg(QB_ENABLED=True, DOWN_PATH="", ANIME_DOWN_PATH="")
    with clean_tables.get_session() as s:
        a = Anime(title="x", season=1, quarter="26C", confirmed=True, rejected=False, total_episodes=12, bangumi_id=1)
        s.add(a); s.commit(); s.refresh(a)
        t = AnimeTorrent(anime_id=a.id, info_hash="c" * 40, raw_title="x - 01", episode=1, status="pending",
                         download_url="https://x/t.torrent")
        s.add(t); s.commit()
        tid = t.id
    assert asyncio.run(A.download_anime_torrent(tid)) is None
    with clean_tables.get_session() as s:
        t = s.get(AnimeTorrent, tid)
        assert t.status == "pending" and "工作目录" in t.fail_reason


def test_stalled_torrents_are_rechecked(clean_tables, cfg, monkeypatch):
    """停滞落定后就脱离了同步：没配完成回调时，qB 那边下完/恢复了，库里也永远是停滞。归档轮里复查一次。"""
    import asyncio
    from core import engine
    from db.models import Anime, AnimeTorrent
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True)
    with clean_tables.get_session() as s:
        a = Anime(title="x", season=1, quarter="26C", confirmed=True, rejected=False, total_episodes=12)
        s.add(a); s.commit(); s.refresh(a)
        ids = []
        for i, prog in enumerate((0.3, 0.3, 0.3)):
            t = AnimeTorrent(anime_id=a.id, info_hash=f"{i + 1:040x}", raw_title=f"x {i}", episode=i + 1,
                             status="stalled", qb_progress=prog, fail_reason="长期无进展")
            s.add(t); s.commit()
            ids.append(t.id)

    async def info(hs):
        return {f"{1:040x}": {"state": "stoppedUP", "progress": 1.0},      # 下完了
                f"{2:040x}": {"state": "downloading", "progress": 0.6},    # 恢复推进
                f"{3:040x}": {"state": "stalledDL", "progress": 0.3}}      # 还是没动
    monkeypatch.setattr(engine.qb, "torrents_info", info)
    assert asyncio.run(engine.recheck_stalled()) == 2
    with clean_tables.get_session() as s:
        got = [(s.get(AnimeTorrent, i).status, s.get(AnimeTorrent, i).qb_progress) for i in ids]
    assert got == [("sent", 1.0), ("sent", 0.6), ("stalled", 0.3)]


def test_zero_progress_timeout_becomes_error_partial_stays_stalled(clean_tables, cfg, monkeypatch):
    """死种（没人做种）一天 0%：落 error，换源兜底才会接手（stalled 算"这一集已有"，永不换源，这一集就漏了）。
    下了一部分的照旧落 stalled（盘上有半成品，留给人工）。"""
    import asyncio
    from datetime import datetime, timedelta
    from core import engine
    from db.models import Anime, AnimeTorrent
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, QB_STALL_TIMEOUT_MIN=1440, QB_SYNC_INTERVAL=30)
    now = datetime.now()
    with clean_tables.get_session() as s:
        a = Anime(title="x", season=1, quarter="26C", confirmed=True, rejected=False, total_episodes=12)
        s.add(a); s.commit(); s.refresh(a)
        ids = []
        for i, prog in enumerate((0.0, 0.4)):
            t = AnimeTorrent(anime_id=a.id, info_hash=f"{i + 1:040x}", raw_title=f"x {i}", episode=i + 1,
                             status="sent", qb_progress=prog, qb_state="stalledDL",
                             qb_synced_at=now - timedelta(seconds=30), qb_progress_at=now - timedelta(days=2))
            s.add(t); s.commit()
            ids.append(t.id)

    async def info(hs):
        return {h: {"state": "stalledDL", "progress": p, "dlspeed": 0, "size": 1}
                for h, p in ((f"{1:040x}", 0.0), (f"{2:040x}", 0.4))}
    monkeypatch.setattr(engine.qb, "torrents_info", info)
    asyncio.run(engine.sync_qb_status(AnimeTorrent))
    with clean_tables.get_session() as s:
        assert [s.get(AnimeTorrent, i).status for i in ids] == ["error", "stalled"]


def test_enrich_also_searches_names_from_torrent_titles(clean_tables, monkeypatch):
    """待识别的番只剩洗过的 title（ANi 的台湾译名『赤河恋影』），bgm 上叫『天是红河岸』：
    重新识别要把种子标题里的罗马音一起拿去搜（真库 #166）。日期基准挑有正常集号的那条。"""
    import asyncio
    from datetime import datetime
    from core import anime as A
    from db.models import Anime, AnimeTorrent
    with clean_tables.get_session() as s:
        a = Anime(title="赤河恋影", season=1, quarter="26D", confirmed=False, rejected=False)
        s.add(a); s.commit(); s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="d" * 40, episode=16.0, status="pending",
                           raw_title="[ANi] Sora wa Akai Kawa no Hotori /  赤河恋影 - 16 [1080P][Baha][WEB-DL][CHT][MP4]",
                           release_time=datetime(2026, 10, 18), created_at=datetime(2026, 10, 18)))
        s.add(AnimeTorrent(anime_id=a.id, info_hash="e" * 40, episode=-2.0, status="pending",
                           raw_title="[ANi] 赤河恋影 PV [1080P]", created_at=datetime(2026, 10, 19)))
        s.commit()
        aid = a.id
    seen = {}

    async def fake_resolve(names, release, ep, h):
        seen.update(names=list(names), ep=ep)
        return None
    monkeypatch.setattr(A.enrich, "resolve", fake_resolve)
    asyncio.run(A.enrich_anime(aid))
    assert "Sora wa Akai Kawa no Hotori" in seen["names"] and seen["names"][0] == "赤河恋影"
    assert seen["ep"] == 16.0, "日期基准用有正常集号的那条，不用 PV"


def test_unmatched_shows_that_used_up_retries_get_one_more_round(clean_tables, cfg):
    from sqlmodel import select
    from core import anime as A
    from db import get_meta_session
    from db.models import Anime, Setting
    cfg(REENRICH_MAX_TRIES=5)
    with get_meta_session() as s:
        for k in list(s.exec(select(Setting).where(Setting.key.like("_UNMATCHED_RETRY_R38%")))):
            s.delete(k)
        s.commit()
    with clean_tables.get_session() as s:
        a = Anime(title="赤河恋影", season=1, quarter="26D", confirmed=False, rejected=False, enrich_tries=5)
        b = Anime(title="被忽略的", season=1, quarter="26D", confirmed=True, rejected=True, enrich_tries=5)
        s.add(a); s.add(b); s.commit()
        aid, bid = a.id, b.id
    assert A.retry_unmatched_once_after_upgrade() == 1
    assert A.retry_unmatched_once_after_upgrade() == 0, "只做一次"
    with clean_tables.get_session() as s:
        assert (s.get(Anime, aid).enrich_tries, s.get(Anime, bid).enrich_tries) == (0, 5)
