"""qB 实时态同步的状态机（第 1 轮 P0 修复的回归网）。

这段逻辑守的是一条【会自我复制文件】的故障链：
  qB 里查不到某个在下的种子 → 判成 error → 该集掉出 HAVE_STATUSES →
  flush 当场给这一集放行另一个源 → 同一集两份落进同一目录。
所以"什么时候才允许判死"必须钉死，不能靠人肉记忆。
用 mock 掉 qb.torrents_info，不打真实网络。
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import config
from core import engine
from db.models import Anime, AnimeTorrent


@pytest.fixture
def torrents(clean_tables):
    """建 n 条"已交付、在下"的种子。返回一个 make(n) 函数。"""
    def make(n, synced_ago_min=5):
        with clean_tables.get_session() as s:
            a = Anime(title="T", season=1, confirmed=True)
            s.add(a)
            s.commit()
            s.refresh(a)
            now = datetime.now()
            for i in range(n):
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i:040x}", raw_title=f"t{i}",
                                   episode=i + 1, status="sent", qb_progress=0.4,
                                   qb_state="downloading", created_at=now,
                                   qb_synced_at=now - timedelta(minutes=synced_ago_min)))
            s.commit()
    return make


def _states(db):
    with db.get_session() as s:
        return {t.info_hash[-2:]: (t.status, t.qb_state) for t in s.exec(select(AnimeTorrent))}


def _alive(hashes):
    return {h: {"state": "downloading", "progress": 0.5, "dlspeed": 1, "size": 1} for h in hashes}


@pytest.fixture
def qb_returns(monkeypatch):
    """让 qb.torrents_info 返回指定内容。"""
    def _set(payload):
        async def fake(hashes):
            return payload(hashes) if callable(payload) else payload
        monkeypatch.setattr(engine.qb, "torrents_info", fake)
    return _set


@pytest.fixture(autouse=True)
def qb_on(cfg):
    cfg(QB_ENABLED=True, QB_SYNC_STATUS=True, QB_SYNC_INTERVAL=30)


async def test_mass_absence_suppresses_failure_not_the_whole_round(clean_tables, torrents, qb_returns):
    """qB 重启装载 resume-data 时，torrents/info 会合法返回 200 + 【不完整】列表——
    既不是 None 也不是空 dict，两个既有的闸都挡不住它。整批一起消失必须【抑制判失败】。

    但只能抑制"判失败"这一步，【不能整轮 return】：判据只看本轮比例，而"用户在 qB 里一次性
    删掉大半在下的种子"是【稳态】缺席，每一轮都会命中闸门 → 缺席行永不落定、恒满足 in-flight、
    同步循环永不休眠，连【在场的健康行】也被连坐、进度冻在最后一次同步的值。"""
    torrents(5)
    qb_returns(lambda hs: {**_alive(hs[:1]), hs[0]: {"state": "downloading", "progress": 0.77,
                                                     "dlspeed": 9, "size": 1}})
    await engine.sync_qb_status(AnimeTorrent)
    st = _states(clean_tables)
    assert all(s == "sent" for s, _ in st.values()), "批量缺席期一条都不该被判失败"
    assert all(q == "_absent" for h, (_, q) in st.items() if h != "00"), "缺席行仍要打记号"
    with clean_tables.get_session() as s:
        alive = s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == f"{0:040x}")).one()
    assert alive.qb_progress == 0.77, "在场的健康行不该被连坐，进度必须照常同步"


async def test_steady_mass_absence_still_settles_eventually(clean_tables, torrents, qb_returns):
    """批量抑制自己也必须有上限：用户一次性删光时，这些行最终仍要落定，
    否则 in-flight 永不清空、同步循环永不休眠、归档永不发生。"""
    torrents(5)
    qb_returns({})                                 # qB 在线，但这批一个都不在（全被删了）
    await engine.sync_qb_status(AnimeTorrent)
    assert all(q == "_absent" for _, q in _states(clean_tables).values())
    with clean_tables.get_session() as s:          # 把"首次 miss"推到批量宽限之外
        for t in s.exec(select(AnimeTorrent)):
            t.qb_synced_at = datetime.now() - timedelta(hours=2)
            s.add(t)
        s.commit()
    await engine.sync_qb_status(AnimeTorrent)
    assert all(s == "error" for s, _ in _states(clean_tables).values()), "有界宽限到点必须落定"


async def test_single_absence_only_marks_first_round(clean_tables, torrents, qb_returns):
    """零星消失（下完被 remove-on-complete 删掉）走正常路径：先记号，不判死。"""
    torrents(5)
    qb_returns(lambda hs: _alive(hs[1:]))
    await engine.sync_qb_status(AnimeTorrent)
    assert _states(clean_tables)["00"] == ("sent", "_absent")


async def test_grace_is_wall_clock_not_just_round_count(clean_tables, torrents, qb_returns):
    """(R1) 修前只看"连续两轮"，而轮询间隔默认 30s ——"宽限一轮"在墙钟上只有半分钟，
    而 qB 装载动辄几分钟。现在记号 + 墙钟下限【两个条件都要满足】。"""
    torrents(5)
    qb_returns(lambda hs: _alive(hs[1:]))
    await engine.sync_qb_status(AnimeTorrent)      # 第一轮：记号
    await engine.sync_qb_status(AnimeTorrent)      # 紧接着第二轮：时间没到
    assert _states(clean_tables)["00"] == ("sent", "_absent"), "宽限窗口内不该判死"


async def test_absent_does_settle_after_the_wall_clock(clean_tables, torrents, qb_returns):
    """但它【必须】在有限时间内落定：否则该行恒满足 in-flight，同步循环永不休眠。"""
    torrents(5)
    qb_returns(lambda hs: _alive(hs[1:]))
    await engine.sync_qb_status(AnimeTorrent)
    with clean_tables.get_session() as s:                     # 把"首次 miss"推到 20 分钟前
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == f"{0:040x}")).one()
        t.qb_synced_at = datetime.now() - timedelta(minutes=20)
        s.add(t)
        s.commit()
    await engine.sync_qb_status(AnimeTorrent)
    assert _states(clean_tables)["00"][0] == "error"


async def test_first_miss_timestamp_is_not_refreshed(clean_tables, torrents, qb_returns):
    """墙钟下限只有在"时间戳停在首次 miss 那一刻"时才成立。
    每轮都刷新的话它永远到不了点——那正是原注释担心的死循环。"""
    torrents(5)
    qb_returns(lambda hs: _alive(hs[1:]))
    await engine.sync_qb_status(AnimeTorrent)
    with clean_tables.get_session() as s:
        first = s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == f"{0:040x}")).one().qb_synced_at
    for _ in range(3):
        await engine.sync_qb_status(AnimeTorrent)
    with clean_tables.get_session() as s:
        again = s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == f"{0:040x}")).one().qb_synced_at
    assert again == first


async def test_offline_qb_changes_nothing(clean_tables, torrents, qb_returns):
    """连不上（None）与"在线但这批都不在"（空 dict）是两回事：前者本轮不动，后者要走落定流程。"""
    torrents(2)
    qb_returns(None)
    assert await engine.sync_qb_status(AnimeTorrent) == 0
    assert all(q == "downloading" for _, q in _states(clean_tables).values())


async def test_delivering_placeholder_rows_are_skipped(clean_tables, qb_returns):
    """status=downloading 是"已置位、还没 add 进 qB"的占位行，交付协程独占它。
    把它当"在下的"去问 qB，会在两轮后凭自造的证据判成 error，而交付其实还在进行。"""
    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1, confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="d" * 40, raw_title="d", episode=1,
                           status="downloading", created_at=datetime.now()))
        s.commit()
    asked = []
    qb_returns(lambda hs: (asked.extend(hs), {})[1])
    await engine.sync_qb_status(AnimeTorrent)
    assert asked == [], "交付中的占位行不该被拿去问 qB"
    assert _states(clean_tables)["dd"][0] == "downloading"


async def test_completion_callback_is_not_overwritten_by_a_stale_snapshot(
        clean_tables, torrents, qb_returns, monkeypatch):
    """await 期间 /api/qb/done 可能把这一行标成"已下完"。
    此时手里的 qB 快照是 await 【之前】发出的，无条件覆写会让 UI 从"已下完"倒退，
    下一轮还会因为 d is None 被误标 error。"""
    torrents(1)
    h = f"{0:040x}"

    async def fake(hashes):
        engine.mark_done_by_hash(h)               # 模拟 await 期间到达的完成回调
        return {h: {"state": "downloading", "progress": 0.5, "dlspeed": 1, "size": 1}}
    monkeypatch.setattr(engine.qb, "torrents_info", fake)
    await engine.sync_qb_status(AnimeTorrent)
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == h)).one()
    assert t.qb_progress >= 1.0 and t.status == "sent"


async def test_sync_is_serialized(clean_tables, torrents, monkeypatch):
    """(R1) 后台轮询与页面『立刻刷新』打的是同一个函数。并发跑时两轮会交错：
    第一轮刚写下记号，第二轮立刻读到它就判死——"宽限"在墙钟上塌成零秒。"""
    import asyncio
    torrents(5)
    concurrent = []

    async def slow(hashes):
        concurrent.append(engine.sync_busy(AnimeTorrent))
        await asyncio.sleep(0.05)
        return _alive(hashes[1:])
    monkeypatch.setattr(engine.qb, "torrents_info", slow)
    await asyncio.gather(engine.sync_qb_status(AnimeTorrent), engine.sync_qb_status(AnimeTorrent))
    assert _states(clean_tables)["00"] == ("sent", "_absent"), "两轮串行后仍应只写到记号"
    assert len(concurrent) == 2 and all(concurrent), "两轮都应在持锁状态下运行"


async def test_disabled_qb_is_a_noop(clean_tables, torrents, qb_returns, cfg):
    torrents(2)
    cfg(QB_ENABLED=False)
    qb_returns({})
    assert await engine.sync_qb_status(AnimeTorrent) == 0


async def test_missing_files_rows_are_rechecked(clean_tables, torrents, qb_returns):
    """(R3) missingFiles 属落定态、不进 in-flight（对的：不该把循环钉醒、也不该被判失败）。
    但排除得太干净就成了死胡同：用户在 qB 里把文件放回去重新校验后，我们再也不看它一眼，
    UI 上那条『文件缺失』告警永不消失、该行也永不归档。所以要跟着本轮一起刷新。"""
    torrents(1)                                   # 一条正常在下的，用来触发本轮同步
    with clean_tables.get_session() as s:
        a = s.exec(select(AnimeTorrent)).one().anime_id
        s.add(AnimeTorrent(anime_id=a, info_hash="f" * 40, raw_title="missing", episode=99,
                           status="sent", qb_progress=1.0, qb_state="missingFiles",
                           qb_synced_at=datetime.now(), created_at=datetime.now()))
        s.commit()
    asked = []

    async def fake(hashes):
        asked.extend(hashes)
        return {h: {"state": "uploading", "progress": 1.0, "dlspeed": 0, "size": 1} for h in hashes}
    engine.qb.torrents_info = fake
    await engine.sync_qb_status(AnimeTorrent)
    assert "f" * 40 in asked, "文件缺失的行也要问一遍 qB"
    assert _states(clean_tables)["ff"][1] == "uploading", "qB 侧修好后告警应自动消失"


async def test_missing_files_row_is_never_settled_by_absence(clean_tables, torrents, qb_returns):
    """(R3) 【补查最容易搞砸的地方】文件缺失的种子 qb_progress 常常正是 1.0。
    如果让它走"qB 查不到就落定"的那套判据，第一轮就会被清成『已下完』——
    UI 从『文件缺失』变『已交付』、随后被归档、集去重认定该集已有一份，
    而盘上根本没有文件，全程不报错。qB 查不到一条本就报文件缺失的种子，唯一正确结论是维持原状。"""
    torrents(1)
    with clean_tables.get_session() as s:
        a = s.exec(select(AnimeTorrent)).first().anime_id
        s.add(AnimeTorrent(anime_id=a, info_hash="f" * 40, raw_title="missing", episode=99,
                           status="sent", qb_progress=1.0, qb_state="missingFiles",
                           qb_synced_at=datetime.now() - timedelta(days=3),
                           created_at=datetime.now()))
        s.commit()
    qb_returns(lambda hs: _alive([h for h in hs if h != "f" * 40]))   # qB 查不到那条
    await engine.sync_qb_status(AnimeTorrent)
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == "f" * 40)).one()
    assert t.qb_state == "missingFiles" and t.status == "sent", "查不到就该维持原状，不能改写"


async def test_missing_files_is_not_stall_timed(clean_tables, torrents, qb_returns, cfg):
    """(R3) missingFiles 属"qB 没在推进它"（文件都找不到了），停滞计时不该走。
    否则默认 24 小时后它会从精确的『文件缺失』变成含糊的『⚠️停滞』——既丢诊断，
    又因为 stalled ∉ TRACKED_STATUSES 而永久退出补查，用户在 qB 修好文件也再无出口。"""
    cfg(QB_STALL_TIMEOUT_MIN=1)
    torrents(1)
    with clean_tables.get_session() as s:
        a = s.exec(select(AnimeTorrent)).first().anime_id
        s.add(AnimeTorrent(anime_id=a, info_hash="f" * 40, raw_title="missing", episode=99,
                           status="sent", qb_progress=0.42, qb_state="missingFiles",
                           qb_progress_at=datetime.now() - timedelta(days=3),
                           qb_synced_at=datetime.now() - timedelta(days=3),
                           created_at=datetime.now()))
        s.commit()
    qb_returns(lambda hs: {h: {"state": "missingFiles", "progress": 0.42, "dlspeed": 0, "size": 1}
                           for h in hs})
    await engine.sync_qb_status(AnimeTorrent)
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent).where(AnimeTorrent.info_hash == "f" * 40)).one()
    assert t.status == "sent", "文件缺失不该被计时成停滞"


async def test_recheck_rows_are_not_in_the_batch_absence_denominator(clean_tables, torrents, qb_returns):
    """批量缺席闸算的是"在下的种子里有多少不见了"。补查行本就可能查不到，
    把它们算进分母会让闸门被无关的行推过阈值。"""
    torrents(3)
    with clean_tables.get_session() as s:
        a = s.exec(select(AnimeTorrent)).first().anime_id
        for i in range(5):
            s.add(AnimeTorrent(anime_id=a, info_hash=f"f{i:039x}", raw_title=f"m{i}", episode=50 + i,
                               status="sent", qb_progress=1.0, qb_state="missingFiles",
                               qb_synced_at=datetime.now(), created_at=datetime.now()))
        s.commit()
    live = [f"{i:040x}" for i in range(3)]
    qb_returns(lambda hs: _alive([h for h in hs if h in live]))   # 3 条在下全在、5 条补查全不在
    await engine.sync_qb_status(AnimeTorrent)
    st = _states(clean_tables)
    assert all(q != "_absent" for h, (_, q) in st.items() if h.startswith("0")), \
        "在下的都在场，不该因为补查行缺席而被当成批量缺席"


async def test_missing_files_alone_does_not_wake_qb(clean_tables, qb_returns):
    """但没有别的在下种子时，不为它单独唤醒 qB——恢复文件是人工动作，不急这一轮。"""
    with clean_tables.get_session() as s:
        a = Anime(title="T", season=1, confirmed=True)
        s.add(a)
        s.commit()
        s.refresh(a)
        s.add(AnimeTorrent(anime_id=a.id, info_hash="f" * 40, raw_title="missing", episode=1,
                           status="sent", qb_progress=1.0, qb_state="missingFiles",
                           qb_synced_at=datetime.now(), created_at=datetime.now()))
        s.commit()
    asked = []

    async def fake(hashes):
        asked.extend(hashes)
        return {}
    engine.qb.torrents_info = fake
    assert await engine.sync_qb_status(AnimeTorrent) == 0
    assert asked == []


async def test_manual_refresh_rechecks_missing_files_with_nothing_in_flight(
        clean_tables, torrents, qb_returns):
    """人点『立刻刷新』时，即使一条在下的都没有也要补查『文件缺失』（D-10）。

    搭车口径（只在有在下种子时顺带查）在这个场景下恰好不成立：用户是等番都下完了才去
    修文件的，此刻没有任何在下种子。于是那条告警要挂到下一次有新种子在下时才消失——
    休播期可能是几周。而"人刚点了刷新"正是该单独唤醒 qB 的时机。
    """
    torrents(1)
    with clean_tables.get_session() as s:
        row = s.exec(select(AnimeTorrent)).one()
        aid = row.anime_id
        row.status = "sent"                       # 把唯一那条在下的落定掉：现在零 in-flight
        row.qb_state = "uploading"
        s.add(AnimeTorrent(anime_id=aid, info_hash="f" * 40, raw_title="missing", episode=99,
                           status="sent", qb_progress=1.0, qb_state="missingFiles",
                           qb_synced_at=datetime.now(), created_at=datetime.now()))
        s.commit()
    asked = []

    async def fake(hashes):
        asked.extend(hashes)
        return {h: {"state": "uploading", "progress": 1.0, "dlspeed": 0, "size": 1} for h in hashes}
    engine.qb.torrents_info = fake

    # 【必须走 core.anime 那层，不能直接调 engine】页面点的是 anime.sync_qb_status(manual=True)，
    # 而直接调 engine.sync_qb_status(..., manual=True) 是在测我自己传进去的参数：
    # 把 anime 那层的 manual=manual 删掉，用例照样绿。实测过，所以改成走真实调用点。
    from core import anime as A
    await A.sync_qb_status()                     # 后台轮次：不为它单独唤醒 qB
    assert asked == [], f"后台轮次不该在零在下种子时打 qB：{asked}"

    await A.sync_qb_status(manual=True)          # 人工刷新（页面按钮走的就是这个）
    assert "f" * 40 in asked, "人工刷新没有补查文件缺失的行"
    assert _states(clean_tables)["ff"][1] == "uploading", "qB 侧修好后告警应自动消失"


async def test_downtime_is_not_counted_as_a_stalled_torrent(clean_tables, torrents, cfg):
    """qB 或整机停机之后的第一轮，不能把在下种子当成「长期无进度」。

    qB 不可达时 sync 走 `info is None` 分支、**一个字段都不写**；autorss 自己停机时更是
    一轮都没跑。两种情况下 `qb_progress_at` 都冻在断档之前，恢复后第一轮
    `now - qb_progress_at` 当场超过阈值（默认 1 天）。后果是批量的：
    关机一天后开机，进程做的第一件事就是把**所有**在下种子标成 stalled，
    而 stalled ∉ TRACKED_STATUSES ⇒ sync 再也不看它们（哪怕 qB 已经在全速下），
    stalled ∈ HAVE_STATUSES ⇒ 集去重挡着不换源，还推一条内容是错的告警。
    """
    cfg(QB_STALL_TIMEOUT_MIN=1440)
    torrents(1)
    old = datetime.now() - timedelta(days=2)
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent)).one()
        t.status, t.qb_progress = "sent", 0.42
        t.qb_progress_at = old          # 进度基准停在两天前
        t.qb_synced_at = old            # 【关键】上一次同步也在两天前 = 我们没在看
        s.commit()

    async def fake(hashes):             # qB 回来了，种子好端端在下
        return {h: {"state": "downloading", "progress": 0.42, "dlspeed": 3_000_000, "size": 1}
                for h in hashes}
    engine.qb.torrents_info = fake
    await engine.sync_qb_status(AnimeTorrent)

    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent)).one()
    assert t.status != "stalled", "停机时间被算到种子头上了"
    assert t.qb_progress_at is not None and t.qb_progress_at > old, "停滞计时没有重新起算"


async def test_real_stall_is_still_detected(clean_tables, torrents, cfg):
    """而真正卡死的种子仍要标停滞——观测一直是新鲜的，只有进度不动。"""
    cfg(QB_STALL_TIMEOUT_MIN=1440)
    torrents(1)
    now = datetime.now()
    with clean_tables.get_session() as s:
        t = s.exec(select(AnimeTorrent)).one()
        t.status, t.qb_progress = "sent", 0.42
        t.qb_progress_at = now - timedelta(days=2)   # 两天没推进
        t.qb_synced_at = now - timedelta(seconds=30)  # 但我们一直在看
        s.commit()

    async def fake(hashes):
        return {h: {"state": "stalledDL", "progress": 0.42, "dlspeed": 0, "size": 1}
                for h in hashes}
    engine.qb.torrents_info = fake
    await engine.sync_qb_status(AnimeTorrent)

    with clean_tables.get_session() as s:
        assert s.exec(select(AnimeTorrent)).one().status == "stalled", "真卡死的没被标出来"


@pytest.mark.parametrize("mid_state", ["checkingUP", "checkingResumeData", "moving"])
async def test_missing_files_row_survives_a_transient_state(clean_tables, torrents, mid_state):
    """`missingFiles` 的行被采样到一次中间态之后，仍要继续被跟踪。

    这条路径是用户按**设计好的修复方式**走出来的：在 qB 里把文件放回去 → 强制重新校验 →
    点页面『立刻刷新』。qB 此刻回的正是 `checkingUP`。
    以前三处闸都在比字面量 "missingFiles"，于是覆写之后这一行【同时】失去两个身份：
    不在 in-flight（被当成落定），也不在补查名单 ⇒ **再没有任何路径问过 qB 它怎么样了**。
    后果分两支：校验成功 → UI 永久停在『校验中』；校验失败/文件仍缺 → 那条『文件缺失』告警
    永不再出现，而行仍是 sent ∈ HAVE_STATUSES，集去重认定盘上有一份 ⇒ 既不重下也不报警。
    """
    torrents(1)
    with clean_tables.get_session() as s:
        row = s.exec(select(AnimeTorrent)).one()
        row.status, row.qb_progress = "sent", 1.0
        row.qb_state, row.qb_synced_at = mid_state, datetime.now()
        s.commit()

    asked = []

    async def fake(hashes):
        asked.extend(hashes)
        return {h: {"state": "uploading", "progress": 1.0, "dlspeed": 0, "size": 1} for h in hashes}
    engine.qb.torrents_info = fake

    await engine.sync_qb_status(AnimeTorrent, manual=True)
    assert asked, f"处在 {mid_state} 的行没有被任何一条路径问过 qB"
    with clean_tables.get_session() as s:
        assert s.exec(select(AnimeTorrent)).one().qb_state == "uploading"


async def test_transient_rows_are_not_archived(clean_tables, torrents, cfg):
    """处在中间态的行不能被归档——归档＝从 qB 移除种子，等于端掉唯一的修复入口。"""
    torrents(1)
    cfg(QB_ARCHIVE_AFTER_DAYS=1)
    with clean_tables.get_session() as s:
        row = s.exec(select(AnimeTorrent)).one()
        row.status, row.qb_progress = "sent", 1.0
        row.qb_state = "checkingUP"
        row.qb_synced_at = datetime.now() - timedelta(days=30)
        s.commit()

    deleted = []
    engine.qb.delete = lambda hs, delete_files=False: _async(bool(deleted.extend(hs)) or True)
    n = await engine.archive_old_completed()
    assert n == 0 and deleted == [], "校验中的行被归掉了"
