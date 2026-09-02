"""完结判定与断更提醒。

完结判据的失败方向是不对称的：**误判成完结 = 最后一集永远下不下来**（而且没有任何报错），
判不出来只是少一个徽标。所以判据必须保守，这一组就是钉住"保守到什么程度"。
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeTorrent


@pytest.fixture(autouse=True)
def _fresh_notify_state():
    """每条用例前清掉通知的进程内记忆。

    `sweep_finished` 现在要问 `notify.cooldown_active`（区分"被冷却挡下"与"没送出去"，
    见 core/anime.py 那段四种含义的说明），而 `_last_sent` 是【模块级】字典、不随用例重置。
    番 id 在各条用例里是从 1 开始的小整数、会重复，于是上一条用例送出去的那条冷却
    会渗进下一条 —— 表现是"推送坏着却落了标记"，而两条用例单独跑都是绿的。
    """
    from services import notify as _N
    _N._last_sent.clear()
    yield
    _N._last_sent.clear()


@pytest.fixture
def make(clean_tables):
    """建一部番 + 若干集种子。eps 是 [(集号, 状态)]。"""
    seq = [0]

    def _mk(total=12, eps=(), progress=1.0, **kw):
        """eps 是 [(集号, 状态)]；progress 是这些种子的 qb_progress。

        默认 1.0＝【真下完】——完结判据要求它（sent 只表示"已交给 qB"，不等于下完了）。
        要测"交了但没下完"就传 progress=0.5。
        """
        with clean_tables.get_session() as s:
            a = Anime(title=f"番{seq[0]}", season=1, confirmed=True, quarter="26C",
                      total_episodes=total, **kw)
            s.add(a)
            s.commit()
            s.refresh(a)
            for ep, st in eps:
                seq[0] += 1                      # info_hash 全局唯一，不能按番内序号生成
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{seq[0]:040x}", raw_title=f"t{ep}",
                                   episode=ep, status=st, qb_progress=progress,
                                   created_at=datetime.now()))
            s.commit()
            seq[0] += 1
            return a.id
    return _mk


def _a(db, aid):
    with db.get_session() as s:
        return s.get(Anime, aid)


# ---------------- 判据 ----------------

def test_all_episodes_downloaded_is_finished(clean_tables, make):
    aid = make(3, [(1, "sent"), (2, "sent"), (3, "stalled")])   # 三条 qb_progress 都是 1.0
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is True


def test_handed_to_qb_but_not_downloaded_is_not_finished(clean_tables, make):
    """(R4) 【判据比集去重闸更严】sent 只表示"已交给 qB"，downloading/stalled 更是明摆着没下完。
    集去重问的是"要不要再下一份"，在下的也算有；完结问的是"是不是真的下完了"。
    用 qb_progress >= 1.0 —— 那是本项目已有的"真下完"信号（归档条件用的就是它），
    且关跟踪时 settle_sent 也会直接写 1.0，不是新发明的口径。"""
    aid = make(2, [(1, "sent"), (2, "sent")], progress=0.5)
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is False
    aid2 = make(2, [(1, "sent"), (2, "downloading")], progress=0.99)
    assert A.is_finished(_a(clean_tables, aid2), A.episode_coverage([aid2]).get(aid2, set())) is False


def test_a_hole_is_not_finished(clean_tables, make):
    aid = make(3, [(1, "sent"), (3, "sent")])
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is False


def test_deleted_does_not_count_as_in_hand(clean_tables, make):
    """用户特意删掉的那一集不该算"已有"——与 restore_anime 同口径。"""
    aid = make(2, [(1, "sent"), (2, "deleted")])
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is False


def test_pending_does_not_count(clean_tables, make):
    """待下不是"到手"。"""
    aid = make(2, [(1, "sent"), (2, "pending")])
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is False


def test_half_episode_does_not_pad_the_count(clean_tables, make):
    """(设计要点) 小数集 11.5 是插入话，bgm 的 total_episodes 不含它。
    按"计数 ≥ T"判会把"1..11 + 11.5"当成 12 集，而第 12 集其实还没下。"""
    aid = make(12, [(i, "sent") for i in range(1, 12)] + [(11.5, "sent")])
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is False


def test_specials_and_unknown_do_not_pad(clean_tables, make):
    aid = make(3, [(1, "sent"), (2, "sent"), (-1, "sent"), (-2, "sent")])
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is False


def test_extra_episodes_beyond_total_are_fine(clean_tables, make):
    """多出来的集号不影响——只要 1..T 齐了就是齐了。"""
    aid = make(2, [(1, "sent"), (2, "sent"), (3, "sent")])
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is True


def test_unknown_total_is_never_finished(clean_tables, make):
    for total in (None, 0):
        aid = make(total, [(1, "sent"), (2, "sent")])
        assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is False


def test_ambiguous_range_is_never_judged(clean_tables, make):
    """(设计要点) 歧义段上绝对编号与季内编号取值域重叠，绝对源的第 O+k 集会在集号维度上
    冒充季内第 O+k 集 —— coverage 是【假的】，会把只下了半季的番判成完结。一律不判。"""
    aid = make(24, [(i, "sent") for i in range(1, 25)], ep_offset=12)
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is False


def test_still_airing_is_never_judged(clean_tables, make):
    """(R18) 一部 12 集的周更番，首播才过 8 周，它就是不可能已经播完 —— coverage 说满了也是假的。

    这一条补的是 ambiguous_range 的另一半：那道守卫要求 `a.ep_offset` 非空，
    也就是只在系统【已经学到 offset】时才说话；而两套编号并存最凶险的情形恰恰是没学到 offset
    （一条都不折算、不按 (集号,源) 去重，库里生生躺着两套编号）。

    真库实证：2026-08-31 06:47 那一轮 sweep 把两部 2026 年 7 月新番判成了完结——
    anime#6（total=12、ep_offset=None、手里 1..33 集）与 anime#60（手里 1..20 集），
    两部都只播了 8 周。加上本判据后全库回扫：挡下的正好就是这两部，其余 57 个候选零影响。
    """
    from datetime import date
    recent = (date.today() - timedelta(weeks=8)).isoformat()
    aid = make(12, [(i, "sent") for i in range(1, 13)], air_date=recent)
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is False, \
        "首播才 8 周的 12 集番被判成了完结"


def test_finished_airing_is_still_judged(clean_tables, make):
    """反向：已经播完的番照常判 —— 这道闸不能顺手把正常的完结判定一起挡了。"""
    from datetime import date
    old = (date.today() - timedelta(weeks=30)).isoformat()
    aid = make(12, [(i, "sent") for i in range(1, 13)], air_date=old)
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is True


def test_unknown_air_date_keeps_the_old_behaviour(clean_tables, make):
    """air_date 不明时不拦（老番/bgm 没给日期的都属于这一类），保持原行为。"""
    aid = make(2, [(1, "sent"), (2, "sent")])          # make 不传 air_date
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is True


async def test_wrong_finish_flag_is_revoked(clean_tables, make, cfg):
    """(R18) 上一版代码误判写下的 finished_at，下一轮巡检自己撤掉，不用人工改库。"""
    from datetime import date
    cfg(ANIME_FINISH_ENABLED=True)
    recent = (date.today() - timedelta(weeks=8)).isoformat()
    aid = make(12, [(i, "sent") for i in range(1, 13)], air_date=recent)
    with clean_tables.get_session() as s:
        s.get(Anime, aid).finished_at = datetime.now()
        s.commit()
    await A.sweep_finished()
    with clean_tables.get_session() as s:
        assert s.get(Anime, aid).finished_at is None, "误判的完结标记没有被撤销"


@pytest.mark.parametrize("total,extra,want,why", [
    (12, 0, True, "没有超界集号，正常判"),
    (12, 1, True, "多 1 集 = bgm 少记一集，这是真实存在的，不该拦（项目原有用例的场景）"),
    (12, 2, True, "多 2 集同上，仍算 bgm 少记"),
    (12, 3, False, "多 3 集起＝库里并存着两套编号，coverage 数出来的『齐』没有意义"),
    (12, 8, False, "真库 anime#60 就是这个形状（超界 8 集）"),
    (12, 21, False, "真库 anime#6（超界 21 集）"),
])
def test_two_numberings_make_coverage_meaningless(clean_tables, make, total, extra, want, why):
    """(R19) 时间闸只是个定时器；这一条不看时间，只看集号分布。

    `_cannot_have_aired_all` 问的是"这一季播完了没有"，答案会随时间变成"播完了"——
    于是它对一部【集号被污染】的番只保护到最后一集播出为止。真库实测：anime#6 的闸
    2026-09-20 到期、#60 是 09-21，之后同一份假 coverage 会把两部都重新判成完结，
    而让 coverage 变假的那批种子一条都没动。第 19 轮审计把这条揪出来——"3 周定时器不是修复"。

    阈值 3 有实证：真库里有超界集号的 5 部，超界集数是 21/8/8/7/3，全部 ≥ 3；
    而"bgm 少记一集"那个要保护的场景是 1。1~2 这一段在真库上是空的。
    """
    from datetime import date
    old = (date.today() - timedelta(weeks=60)).isoformat()      # 早就播完，把时间闸排除掉
    eps = [(i, "sent") for i in range(1, total + 1)]
    eps += [(total + 1 + j, "sent") for j in range(extra)]
    aid = make(total, eps, air_date=old)
    a = _a(clean_tables, aid)
    assert A._cannot_have_aired_all(a) is False, "前提没摆对：这一条要在时间闸失效的前提下测"
    assert A.is_finished(a, A.episode_coverage([aid]).get(aid, set())) is want, why


def test_optout_is_never_judged(clean_tables, make):
    aid = make(2, [(1, "sent"), (2, "sent")], finish_optout=True)
    assert A.is_finished(_a(clean_tables, aid), A.episode_coverage([aid]).get(aid, set())) is False


# ---------------- 巡检与订阅闸 ----------------

async def test_sweep_marks_and_notifies(clean_tables, make, cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(A, "notify_event",
                        lambda k, m, **kw: (sent.append((k, m)), _async(True))[1])
    cfg(ANIME_FINISH_ENABLED=True)
    aid = make(2, [(1, "sent"), (2, "sent")])
    assert await A.sweep_finished() == 1
    assert _a(clean_tables, aid).finished_at is not None
    assert sent and sent[0][0] == "finished"
    assert await A.sweep_finished() == 0, "第二轮不该重复判定/重复通知"


async def test_finish_mark_is_revoked_when_no_longer_complete(
        clean_tables, make, cfg, monkeypatch):
    """(R4) 【本功能最要紧的性质】判据 episode_coverage 是【瞬时量】——删了一集、某集落 error、
    bgm 重识别后总集数变大，它都会变。而 finished_at 若只增不减，就是拿一个会浮动的判据
    去写一个永久的结论：一次误判之后，停订开着时该番【永久停订】，用户不点按钮就永远好不了。"""
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(True))
    cfg(ANIME_FINISH_ENABLED=True)
    aid = make(2, [(1, "sent"), (2, "sent")])
    assert await A.sweep_finished() == 1
    assert _a(clean_tables, aid).finished_at is not None
    with clean_tables.get_session() as s:      # 用户删掉了第 2 集的文件
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.episode == 2)).one()
        t.status = "deleted"
        s.commit()
    await A.sweep_finished()
    assert _a(clean_tables, aid).finished_at is None, "不再集齐就要撤销标记"


async def test_revoke_respects_optout(clean_tables, make, cfg, monkeypatch):
    """点过『继续订阅』的番两个方向都不碰——那是用户的显式意志。"""
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(True))
    cfg(ANIME_FINISH_ENABLED=True)
    aid = make(2, [(1, "sent"), (2, "sent")], finish_optout=True)
    assert await A.sweep_finished() == 0
    assert _a(clean_tables, aid).finished_at is None


async def test_first_run_backfill_does_not_flood(clean_tables, make, cfg, monkeypatch):
    """(R4) 老库第一次跑到这里时，历史上早已完结的番会被一次性全部判定——
    那不是"刚刚完结"的消息，几十条推送只会把限流额度占满、把真正的新事件挤掉。"""
    sent = []
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: (sent.append(m), _async(True))[1])
    cfg(ANIME_FINISH_ENABLED=True)
    for _ in range(8):
        make(1, [(1, "sent")])
    assert await A.sweep_finished() == 8
    assert sent == [], "首轮回填只打标记，不推送"


async def test_sweep_disabled_does_nothing(clean_tables, make, cfg):
    cfg(ANIME_FINISH_ENABLED=False)
    aid = make(2, [(1, "sent"), (2, "sent")])
    assert await A.sweep_finished() == 0
    assert _a(clean_tables, aid).finished_at is None


def test_unsub_switch_gates_the_download_predicate(clean_tables, make, cfg):
    """【默认不改下载行为】判完结只是标记；停不停订由开关决定。
    这是整个功能里唯一会改变下载行为的地方，所以两个方向都要钉住。"""
    aid = make(2, [(1, "sent"), (2, "sent")])
    with clean_tables.get_session() as s:
        s.get(Anime, aid).finished_at = datetime.now()
        s.commit()
    a = _a(clean_tables, aid)
    cfg(ANIME_FINISH_UNSUB=False)
    assert A.is_subscribed(a) is True, "默认不停订"
    assert A.download_plan_for_ids([aid]) is not None
    cfg(ANIME_FINISH_UNSUB=True)
    assert A.is_subscribed(a) is False


def test_subscribed_where_and_is_subscribed_agree(clean_tables, make, cfg):
    """SQL 判据与内存判据必须同口径——本项目历史上的回归几乎都来自"同一个判据两处手抄"。"""
    ids = [make(2, [(1, "sent"), (2, "sent")]) for _ in range(3)]
    with clean_tables.get_session() as s:
        s.get(Anime, ids[0]).finished_at = datetime.now()
        s.get(Anime, ids[1]).rejected = True
        s.commit()
    for unsub in (False, True):
        cfg(ANIME_FINISH_UNSUB=unsub)
        with clean_tables.get_session() as s:
            sql_ids = {a.id for a in s.exec(select(Anime).where(*A.subscribed_where()))}
            mem_ids = {a.id for a in s.exec(select(Anime)) if A.is_subscribed(a)}
        assert sql_ids == mem_ids, f"ANIME_FINISH_UNSUB={unsub} 时两套判据分家了"


def test_resubscribe_sticks(clean_tables, make, cfg):
    """(设计要点) 完结判据是【状态式】的（集齐了就恒为真）。只清 finished_at 的话，
    下一轮巡检立刻又把它判回完结 —— 那个按钮就成了"点了没用"。"""
    cfg(ANIME_FINISH_ENABLED=True)
    aid = make(2, [(1, "sent"), (2, "sent")])
    with clean_tables.get_session() as s:
        s.get(Anime, aid).finished_at = datetime.now()
        s.commit()
    assert A.resubscribe(aid) is True
    a = _a(clean_tables, aid)
    assert a.finished_at is None and a.finish_optout is True
    assert A.is_finished(a, A.episode_coverage([aid]).get(aid, set())) is False


# ---------------- 断更 ----------------

async def test_idle_notifies_once_per_silence(clean_tables, make, cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(A, "notify_event",
                        lambda k, m, **kw: (sent.append((k, m)), _async(True))[1])
    cfg(ANIME_IDLE_DAYS=14)
    aid = make(12, [(1, "sent")])
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent)).one()
        t.created_at = datetime.now() - timedelta(days=30)
        s.commit()
    assert await A.sweep_idle() == 1
    assert sent and sent[0][0] == "idle"
    assert await A.sweep_idle() == 0, "同一段静默期内不该重复提醒"


async def test_idle_does_not_record_when_the_notification_was_dropped(
        clean_tables, make, cfg, monkeypatch):
    """(R4) idle_notified_at 的唯一用途是"这条已经说过了"。通知被限流/未订阅/没配 URL 丢掉时
    还把它记上，等于把这批番的提醒永久吃掉一次（下次要等满一个静默期）——
    而这个提醒本身就是靠它去重的，丢了补不回来。"""
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(False))
    cfg(ANIME_IDLE_DAYS=14)
    aid = make(12, [(1, "sent")])
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent)).one()
        t.created_at = datetime.now() - timedelta(days=30)
        s.commit()
    assert await A.sweep_idle() == 0
    assert _a(clean_tables, aid).idle_notified_at is None, "没发出去就不该记账"
    # 下一轮通知能发了 → 照常提醒
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(True))
    assert await A.sweep_idle() == 1


async def test_recent_torrent_is_not_idle(clean_tables, make, cfg):
    cfg(ANIME_IDLE_DAYS=14)
    make(12, [(1, "sent")])
    assert await A.sweep_idle() == 0


async def test_anime_with_no_torrents_is_not_idle(clean_tables, make, cfg):
    """一条种子都没有的番不是"断更"——它是还没开播/还没收到过，另一回事。"""
    cfg(ANIME_IDLE_DAYS=14)
    make(12, [])
    assert await A.sweep_idle() == 0


async def test_finished_anime_is_not_idle(clean_tables, make, cfg):
    """完结的番本来就该没有新种子了。"""
    cfg(ANIME_IDLE_DAYS=14)
    aid = make(1, [(1, "sent")])
    with clean_tables.get_session() as s:
        s.get(Anime, aid).finished_at = datetime.now()
        t = s.exec(select(AnimeTorrent)).one()
        t.created_at = datetime.now() - timedelta(days=60)
        s.commit()
    assert await A.sweep_idle() == 0


async def test_idle_disabled(clean_tables, make, cfg):
    cfg(ANIME_IDLE_DAYS=0)
    assert await A.sweep_idle() == 0


def _async(v=True):
    """假 notify_event。【默认返回 True】——真实的 event() 返回"这条有没有真发出去"，
    调用方据此决定要不要落"已通知"的标记。返回 None 会被当成"没发出去"。"""
    async def _c():
        return v
    return _c()


async def test_revoke_still_runs_when_detection_is_off(clean_tables, make, cfg, monkeypatch):
    """(R5) 关掉 ANIME_FINISH_ENABLED 只该停"判"，不该停"撤"。

    整个函数早退的话，已经标上的 finished_at 就被永久冻结——而消费侧（订阅闸、仪表盘、徽标）
    看的是【另一个】开关 ANIME_FINISH_UNSUB。于是"关掉完结判定"反而让一批番永久停订，
    且再也没有任何路径能解冻它们。"""
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(True))
    cfg(ANIME_FINISH_ENABLED=True)
    aid = make(2, [(1, "sent"), (2, "sent")])
    await A.sweep_finished()
    assert _a(clean_tables, aid).finished_at is not None
    with clean_tables.get_session() as s:                  # 用户删了一集
        s.exec(select(AnimeTorrent).where(AnimeTorrent.episode == 2)).one().status = "deleted"
        s.commit()
    cfg(ANIME_FINISH_ENABLED=False)                        # 然后把判定关掉
    await A.sweep_finished()
    assert _a(clean_tables, aid).finished_at is None, "撤销是清理动作，任何时候都该跑"


async def test_detection_off_does_not_mark_new_ones(clean_tables, make, cfg, monkeypatch):
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(True))
    cfg(ANIME_FINISH_ENABLED=False)
    aid = make(2, [(1, "sent"), (2, "sent")])
    assert await A.sweep_finished() == 0
    assert _a(clean_tables, aid).finished_at is None


async def test_backfill_flag_cannot_be_rearmed(clean_tables, make, cfg, monkeypatch):
    """(R5) 回填判据必须是"本库【从来】没判过完结"。
    早先的判据是"当前一部都没标记"，而候选集排除了 finish_optout ——
    用户对已完结的番点几次『继续订阅』就能让它重新成立，于是下一批真的完结的番静默无声。"""
    sent = []
    monkeypatch.setattr(A, "notify_event",
                        lambda k, m, **kw: (sent.append(m), _async(True))[1])
    cfg(ANIME_FINISH_ENABLED=True)
    for _ in range(6):
        make(1, [(1, "sent")])
    assert await A.sweep_finished() == 6 and sent == [], "首轮回填不推送"
    for aid in [a.id for a in _all(clean_tables)]:         # 用户对它们全点『继续订阅』
        A.resubscribe(aid)
    for _ in range(6):                                     # 又来一批真的完结的
        make(1, [(1, "sent")])
    assert await A.sweep_finished() == 6
    assert len(sent) == 6, "回填只做一次，之后必须照常推送"


def _all(db):
    with db.get_session() as s:
        return list(s.exec(select(Anime)))


async def test_idle_reports_the_freshest_breakage_first(clean_tables, make, cfg, monkeypatch):
    """(R5) 排序方向曾与自己的注释相反：取静默最久的那几部正是反的——
    那些多半是早就完结的老番，而用户需要立刻知道的是"上周还在更、这周没了"的那一部。"""
    msgs = []
    monkeypatch.setattr(A, "notify_event",
                        lambda k, m, **kw: (msgs.append(m), _async(True))[1])
    cfg(ANIME_IDLE_DAYS=14)
    for days, name in ((50, "老番"), (40, "中番"), (16, "刚断更")):
        aid = make(12, [(1, "sent")])
        with clean_tables.get_session() as s:
            a = s.get(Anime, aid)
            a.title = name
            t = s.exec(select(AnimeTorrent).where(AnimeTorrent.anime_id == aid)).one()
            t.created_at = datetime.now() - timedelta(days=days)
            s.commit()
    assert await A.sweep_idle() == 3
    assert msgs[0].index("刚断更") < msgs[0].index("老番"), f"最近才出事的要排在最前：{msgs[0]}"


def _clear_backfill_mark(key: str) -> None:
    """回填标记在 meta 库里，clean_tables 清的是业务库——跨用例会残留，用例要自己清。"""
    from db import get_meta_session
    from db.models import Setting
    with get_meta_session() as s:
        row = s.get(Setting, key)
        if row is not None:
            s.delete(row)
            s.commit()


async def test_idle_window_does_not_repeat_old_news(clean_tables, make, cfg, monkeypatch):
    """4 倍阈值之外【不再重复】提醒——已经说过一次的老番不该每 N 天原样重发。

    注意断言的是"不重复"而不是"永不提醒"：见下一条用例。窗口的存在是为了让
    "库里躺着的几十部两年前的老番"闭嘴，不是为了让某部番一次都收不到。
    """
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(True))
    cfg(ANIME_IDLE_DAYS=14)
    aid = make(12, [(1, "sent")])
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent)).one()
        t.created_at = datetime.now() - timedelta(days=100)
        a = s.get(Anime, aid)
        a.idle_notified_at = datetime.now() - timedelta(days=90)   # 早就提醒过了
        s.commit()
    assert await A.sweep_idle() == 0


async def test_never_delivered_alert_survives_the_window(clean_tables, make, cfg, monkeypatch):
    """从未成功送达过的番，即使已滑出窗口也要补送一次，且只补一次（D-20）。

    这是窗口原本的代价：通知在整段静默期里一直没送出去（NOTIFY_URL 还没配、被限流吞掉、
    推送服务挂了），番就悄悄滑出窗口、用户从头到尾收不到任何消息——而"刚装好还没配推送"
    恰恰是源失效最容易发生的时候。
    """
    sent = []
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(bool(sent.append(m)) or True))
    cfg(ANIME_IDLE_DAYS=14)
    A._mark_backfilled(A._IDLE_BACKFILL_KEY)      # 首轮回填已做过（存量老番不参与）
    aid = make(12, [(1, "sent")])
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent)).one()
        t.created_at = datetime.now() - timedelta(days=100)
        s.commit()
    assert await A.sweep_idle() == 1, "从未送达过的提醒被窗口无声吃掉了"
    assert await A.sweep_idle() == 0, "补送之后还在重复发"


async def test_first_run_backfills_old_silence_without_pushing(clean_tables, make, cfg, monkeypatch):
    """功能首次上线时，存量早已静默的老番一次性记账、不推送——否则升级后灌一波。"""
    sent = []
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(bool(sent.append(m)) or True))
    cfg(ANIME_IDLE_DAYS=14)
    _clear_backfill_mark(A._IDLE_BACKFILL_KEY)
    make(12, [(1, "sent")])
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent)).one()
        t.created_at = datetime.now() - timedelta(days=100)
        s.commit()
    assert await A.sweep_idle() == 0
    assert sent == []


def test_total_episodes_change_revokes_the_finished_mark(monkeypatch):
    """bgm 把总集数改了 → 完结标记当场作废，不等下一个巡检周期（D-19）。

    分割播出的番被 bgm 并成一季是常见情形：12 集时判过完结，改成 24 集后那个结论就是错的，
    而开了『完结停订』的话它还会把后 12 集一直挡在门外。
    """
    from datetime import datetime as _dt

    from core import anime as A
    from db.models import Anime

    a = Anime(display_name="某番", total_episodes=12, finished_at=_dt(2026, 1, 1))
    monkeypatch.setattr(A.engine, "apply_bgm_meta",
                        lambda obj, info, keep_path=False: setattr(obj, "total_episodes", 24))
    A._apply_bgm(a, {"total_episodes": 24})
    assert a.finished_at is None, "总集数变了却还留着完结标记"


def test_unchanged_total_keeps_the_finished_mark(monkeypatch):
    """总集数没变就别动它——每轮重识别都清一次的话，完结时刻会被反复重置、通知反复重发。"""
    from datetime import datetime as _dt

    from core import anime as A
    from db.models import Anime

    marked = _dt(2026, 1, 1)
    a = Anime(display_name="某番", total_episodes=12, finished_at=marked)
    monkeypatch.setattr(A.engine, "apply_bgm_meta", lambda obj, info, keep_path=False: None)
    A._apply_bgm(a, {"total_episodes": 12})
    assert a.finished_at == marked


async def test_finish_push_survives_coverage_flapping(clean_tables, make, cfg, monkeypatch):
    """完结推送按番去重：反复翻转不该每翻一次就发一条（D-21）。

    finished_at 只挡得住"标记还在"的重复；删种 / missingFiles / bgm 改总集数都会让它
    撤销后再判定一次，而每一次都是一条"全 N 集已下齐"。
    """
    from services import notify as N
    sent = []
    monkeypatch.setattr(A, "notify_event", N.event)
    monkeypatch.setattr(N, "notify", lambda msg, **kw: _async(bool(sent.append(msg)) or True))
    monkeypatch.setattr(N, "enabled", lambda kind: True)
    N.reset_state()
    cfg(ANIME_FINISH_ENABLED=True, ANIME_FINISH_UNSUB=False)
    aid = make(2, [(1, "sent"), (2, "sent")])
    assert await A.sweep_finished() == 1
    assert len(sent) == 1
    with clean_tables.get_session() as s:                  # 抖一下：标记撤销后再判回来
        s.get(Anime, aid).finished_at = None
        s.commit()
    assert await A.sweep_finished() == 1
    assert len(sent) == 1, f"翻转一次就多推一条：{sent}"


async def test_idle_backfill_also_covers_anime_outside_the_subscription_set(
        clean_tables, make, cfg, monkeypatch):
    """首轮回填要覆盖【全表】，不能只覆盖当轮的订阅集（P2-2）。

    时序问题：worker 里 sweep_finished 就跑在 sweep_idle 前面且默认开着，于是升级当天
    「老且集齐」的番先被打上 finished_at、当场掉出 subs —— 回填对它最想挡的那批番
    覆盖率正好是 0。等它日后被撤销完结标记回到订阅态，就会带着 idle_notified_at=None
    直接命中豁免，报一条 800 天前的假断更。
    """
    sent = []
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(bool(sent.append(m)) or True))
    cfg(ANIME_IDLE_DAYS=14)
    _clear_backfill_mark(A._IDLE_BACKFILL_KEY)
    aid = make(12, [(1, "sent")])
    with clean_tables.get_session() as s:
        s.exec(select(AnimeTorrent)).one().created_at = datetime.now() - timedelta(days=800)
        s.get(Anime, aid).finished_at = datetime.now()      # 已完结 → 不在 subs 里
        s.commit()

    assert await A.sweep_idle() == 0 and sent == []          # 本轮：回填，不推送

    with clean_tables.get_session() as s:                    # 日后被撤销完结标记，回到订阅态
        s.get(Anime, aid).finished_at = None
        s.commit()
    assert await A.sweep_idle() == 0, f"回填漏了它，报出一条 800 天前的假断更：{sent}"
    assert sent == []


async def test_finish_backfill_latch_drops_on_the_first_round_with_hits(
        clean_tables, make, cfg, monkeypatch):
    """「首轮回填不推送」的闩要在【第一轮有命中】时就落下（F10）。

    原写法只在"≥6 命中"那一轮落闩，而那一轮正是被静默的那一轮。于是一个从来没有过
    ≥6 命中的库，闩永远不落；等到季末某一轮真有 6 部同时完结时，那一轮被当成"首轮回填"
    整批静默——finished_at 照写、停订照生效，而用户一条通知都收不到。
    """
    sent = []
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(bool(sent.append(m)) or True))
    cfg(ANIME_FINISH_ENABLED=True)
    _clear_backfill_mark("_FINISH_BACKFILL_DONE")

    make(1, [(1, "sent")])                       # 第 1 轮：只有 1 部完结 → 正常推送并落闩
    assert await A.sweep_finished() == 1
    assert len(sent) == 1

    for _ in range(6):                           # 第 2 轮：6 部同时完结，不该被当成"首轮回填"
        make(1, [(1, "sent")])
    assert await A.sweep_finished() == 6
    assert len(sent) == 7, f"6 部同时完结被整批静默了：{sent}"


# ---------------- 完结通知丢了要能重来（E-32，R20） ----------------

async def test_finish_mark_waits_for_the_push(clean_tables, make, cfg, monkeypatch):
    """(E-32) 推送坏着的时候不落完结标记 —— 否则那条通知就是【永久丢】。

    原来是【整批】commit 完 finished_at 才逐部发通知，而候选过滤卡的正是
    `finished_at is None`：通知丢了下一轮不会重来。R17 加的连续失败熔断把这条放大了
    （连挂 5 条就保证静默 300 秒，那 300 秒里判定的完结全部静默，而 finished_at 照落）。

    【为什么不加 finish_notified_at 那一列】换个顺序就能拿到同样的性质，不用开 revision。
    代价是推送坏着的那段时间 ANIME_FINISH_UNSUB 对这几部暂时不生效——它们会继续下新集，
    那是本项目一贯的安全方向（多下几集 ≪ 停订一部还在更新的番）。
    """
    alive = [False]
    sent = []
    monkeypatch.setattr(A, "notify_enabled", lambda kind: True)   # 用户订阅了完结通知
    monkeypatch.setattr(A, "notify_event",
                        lambda k, m, **kw: (sent.append(m) if alive[0] else None,
                                            _async(alive[0]))[1])
    cfg(ANIME_FINISH_ENABLED=True)
    aid = make(2, [(1, "sent"), (2, "sent")])

    assert await A.sweep_finished() == 0, "推送坏着时不该落标记"
    assert _a(clean_tables, aid).finished_at is None
    assert sent == []

    alive[0] = True                                    # 推送恢复
    assert await A.sweep_finished() == 1, "推送恢复后下一轮要重新判、重新通知"
    assert _a(clean_tables, aid).finished_at is not None
    assert len(sent) == 1


async def test_finish_mark_lands_when_nobody_subscribed(clean_tables, make, cfg, monkeypatch):
    """未订阅/没配 NOTIFY_URL 时 notify_event 恒 False —— 但那不是"丢了"，是"本来就不发"。

    少了这一支就会变成：没配通知的用户，finished_at 永远写不进去、停订永远不生效。
    """
    monkeypatch.setattr(A, "notify_enabled", lambda kind: False)
    monkeypatch.setattr(A, "notify_event", lambda k, m, **kw: _async(False))
    cfg(ANIME_FINISH_ENABLED=True)
    aid = make(2, [(1, "sent"), (2, "sent")])
    assert await A.sweep_finished() == 1, "没订阅通知的用户完结标记落不下去"
    assert _a(clean_tables, aid).finished_at is not None


async def test_cooldown_does_not_block_the_mark_forever(clean_tables, make, cfg, monkeypatch):
    """(R20 回归) 「被 7 天冷却挡下」≠「没送出去」。

    `notify_event` 返回 False 有【四】种含义：① 没送出去（推送坏/熔断/限流）② 未订阅
    ③ **被冷却挡下** ④ ——，而 E-32 那版只分了两种，把 ③ 当成了 ①。

    后果：一部番只要「判完结 → 抖一下被撤销 → 再判完结」，第二次就被 7 天冷却挡住，
    于是 `finished_at` **永远落不下来**、每一轮巡检都重判一次。
    实测复现过：改动前 R3 落标记，改动后 R3/R4/R5 全都不落。
    """
    from services import notify as N
    sent = []
    monkeypatch.setattr(A, "notify_event", N.event)
    monkeypatch.setattr(N, "notify", lambda msg, **kw: _async(bool(sent.append(msg)) or True))
    monkeypatch.setattr(N, "enabled", lambda kind: True)
    monkeypatch.setattr(A, "notify_enabled", lambda kind: True)
    cfg(ANIME_FINISH_ENABLED=True)
    aid = make(2, [(1, "sent"), (2, "sent")])

    assert await A.sweep_finished() == 1 and len(sent) == 1
    with clean_tables.get_session() as s:          # 抖一下：撤销标记
        s.get(Anime, aid).finished_at = None
        s.commit()

    assert await A.sweep_finished() == 1, "被冷却挡下之后完结标记再也落不下来了"
    assert _a(clean_tables, aid).finished_at is not None
    assert len(sent) == 1, f"冷却失效、重复推送了：{sent}"


# ---------------- (R26) 一次性回填的标记必须按【业务库】分账 ----------------

def test_the_backfill_latch_does_not_leak_across_databases(clean_tables, tmp_path):
    """切库之后，"这件一次性的事做过没有"必须重新问一遍。

    这类标记（`_FINISH_BACKFILL_DONE` / `_idle_backfilled` / `_QB_PROGRESS_BACKFILLED`）
    判的对象是**业务库**（注释原文："本库【从来】没判过完结"），
    可它们存在 **meta 库**里 —— 而按双引擎设计 meta 恒留本地、**不随业务库走**。
    两个方向都错：

      · 【标记跟过来了】业务库 A 跑过一轮 → 切到业务库 B（设置页明说"切过去就是那个库里
        已有的内容"）→ B 从没回填过，标记却已是"做过了"，于是 B 里所有
        "最后一条种子早于 ANIME_IDLE_DAYS×4"的番**首轮就发断更告警** ——
        而那批静默是切库**之前**的历史，不是刚发生的，正是这个标记存在的唯一理由。
      · 【标记没跟过来】业务库在 MySQL、本地 meta 被恢复成一份旧备份（backup 文档化的
        恢复流程只换本地文件）→ 标记消失 → 对着一个跑了半年的库重新"首次回填"一遍。
    """
    import db as _db
    from core import anime as A

    assert not A._backfilled(A._IDLE_BACKFILL_KEY), "前提：这个库还没回填过"
    A._mark_backfilled(A._IDLE_BACKFILL_KEY)
    assert A._backfilled(A._IDLE_BACKFILL_KEY), "标记没落下"

    before = _db.engine
    try:
        _db.switch_data_engine(f"sqlite:///{tmp_path/'other.db'}")
        assert not A._backfilled(A._IDLE_BACKFILL_KEY), (
            "切到另一个业务库之后标记还在 —— 那个库从没回填过，"
            "首轮会把它全部历史静默当成『刚发生的断更』推给用户")
        A._mark_backfilled(A._IDLE_BACKFILL_KEY)
    finally:
        _db.switch_data_engine(None if before is _db.meta_engine
                               else str(before.url.render_as_string(hide_password=False)))
    assert A._backfilled(A._IDLE_BACKFILL_KEY), "切回原来的库，之前的标记应该还在"


def test_the_identity_matches_the_same_database_predicate(tmp_path):
    """身份串的判据必须与 `transfer.same_database` 同口径 —— 否则两处会对"是不是同一个库"给出相反答案。

    SQLite 比 realpath（相对路径、软链、`./` 前缀都指向同一个文件），
    其余按 (方言, 主机, 端口, 库名) 比。
    """
    import os

    import sqlalchemy as sa

    import db as _db
    from db import transfer

    f = tmp_path / "x.db"
    f.write_bytes(b"")
    a = sa.create_engine(f"sqlite:///{f}")
    b = sa.create_engine(f"sqlite:///{os.path.join(str(tmp_path), '.', 'x.db')}")
    c = sa.create_engine(f"sqlite:///{tmp_path/'y.db'}")

    assert transfer.same_database(a, b) is True, "前提：两条路径确实指向同一个文件"
    assert _db.data_identity(a) == _db.data_identity(b), \
        "same_database 说是同一个库，身份串却不同 —— 切库回来会白回填一次"
    assert transfer.same_database(a, c) is False
    assert _db.data_identity(a) != _db.data_identity(c), \
        "两个不同的库拿到了同一个身份串 —— 标记会串库"
