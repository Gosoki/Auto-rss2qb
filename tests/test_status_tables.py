"""状态词表的一致性。

本项目自己在 engine 里写着这条纪律：「同一份知识记两处会漂移」——qB 状态词表因此被统一到
一张表里（分类标记 + 中文名同源）。这一组用例把那条纪律变成可执行的断言：
新增一个状态却漏了某一层，用例当场红，而不是等到某个统计"长期少记"才被发现。
"""
import pytest

from core import engine
from pages.layout import STATUS_CN, torrent_status_cn


# ---------------- 应用侧 status ----------------

def test_status_subsets_are_strictly_layered():
    """三个判据是【逐层包含】的，写成累加形式正是为了防止某一层被单独改歪。"""
    assert set(engine.TRACKED_STATUSES) < set(engine.HAVE_STATUSES)
    assert set(engine.HAVE_STATUSES) < set(engine.HANDLED_STATUSES)
    assert set(engine.HANDLED_STATUSES) <= set(engine.ALL_STATUSES)


def test_layer_membership_is_exactly_as_designed():
    """把每一层的成员钉死。改这里之前先想清楚：
    · stalled 不在 TRACKED（已判停滞、脱离轮询、交人工），但在 HAVE（半成品文件确实在盘上）
    · deleted 不在 HAVE（用户删的那条不会自己回来），但在 HANDLED（其被压成 skipped 的兄弟不该复活）"""
    assert set(engine.TRACKED_STATUSES) == {"sent", "downloading"}
    assert set(engine.HAVE_STATUSES) == {"sent", "downloading", "stalled"}
    assert set(engine.HANDLED_STATUSES) == {"sent", "downloading", "stalled", "deleted"}


def test_every_status_has_a_chinese_label():
    """漏一个的表现是 UI 上直接显示英文标识符（STATUS_CN.get 的兜底是原样返回）。"""
    missing = set(engine.ALL_STATUSES) - set(STATUS_CN)
    assert not missing, f"这些状态没有中文名：{missing}"


def test_no_orphan_labels():
    """反向也要成立：UI 词表里不该有 ALL_STATUSES 之外的键（那是删掉状态后的残留）。"""
    assert not set(STATUS_CN) - set(engine.ALL_STATUSES)


@pytest.mark.parametrize("sync_on,progress,synced,expect", [
    (False, 0.0, None, "已下"),        # 关跟踪：发送即视为完成
    (True, 0.0, None, "下载中"),       # 开跟踪、还没同步回来
    (True, 0.5, "yes", "已交付"),      # 同步过了
    (True, 1.0, "yes", "已交付"),
])
def test_sent_label_follows_tracking_mode(cfg, sync_on, progress, synced, expect):
    """sent 的含义随"是否读 qB 实时态"而变，文案必须跟着变——
    关跟踪时说"已交付"会让人以为还没下完。"""
    cfg(QB_SYNC_STATUS=sync_on)
    assert torrent_status_cn("sent", progress, synced) == expect


# ---------------- qB 原始态 ----------------

def test_qb_state_flags_are_all_known():
    """分类标记只有 D/S/T/X/W 五种；写错一个字母不会报错，只会让该状态静默掉出所有集合。"""
    for state, (flags, cn) in engine._QB_STATES.items():
        assert set(flags) <= set("DSTXW"), (state, flags)
        assert cn, state


def test_qb_state_cn_covers_every_state():
    assert set(engine.QB_STATE_CN) == set(engine._QB_STATES)


def test_seeding_states_are_all_settled():
    """做种/完成态必然是落定态（不再需要轮询跟踪）。漏了 X 会让下完的种子永久留在 in-flight，
    同步循环永不休眠、每个活跃间隔空打一次 qB。"""
    assert engine._QB_SEEDING <= engine._QB_SETTLED


def test_missing_files_is_settled_but_not_seeding():
    """文件缺失是终态（不再变）但【不是】已完成——把它算成完成会让归档去删它，
    而归档=从 qB 移除种子，恰恰端掉唯一的修复入口。"""
    assert "missingFiles" in engine._QB_SETTLED
    assert "missingFiles" not in engine._QB_SEEDING


def test_downloading_and_seeding_never_overlap():
    """同一个状态不能既算"在下"又算"已完成"，否则两个统计数字加起来会超过总数。"""
    assert not (engine._QB_DOWNLOADING & engine._QB_SEEDING)


def test_queued_and_paused_are_not_stall_candidates():
    """qB 的 max_active_downloads 一小时，批量补番会有几十条卡在 queuedDL。
    它们不是"卡住"，停滞计时不该走——漏了这条会把整批误标停滞、脱离轮询、报一堆假告警。"""
    for s in ("queuedDL", "pausedDL", "stoppedDL"):
        assert s in engine._QB_NOT_ADVANCING, s
    assert "downloading" not in engine._QB_NOT_ADVANCING


def test_absent_marker_never_collides_with_a_real_qb_state():
    """自造的记号态用下划线开头，保证永不会与 qB 真实返回的词撞。"""
    assert engine._QB_ABSENT.startswith("_")
    assert not any(s.startswith("_") for s in engine._QB_STATES if s != engine._QB_ABSENT)


def test_absent_marker_pauses_stall_timing():
    """qB 都看不到它了，停滞计时自然不该继续走。"""
    assert engine._QB_ABSENT in engine._QB_NOT_ADVANCING


def test_prebuilt_lists_match_their_sets():
    """两个 list 是为热路径预算的副本——它们与源集合漂移了不会报错，只会让 SQL 少滤掉几个状态。"""
    assert set(engine._QB_SETTLED_LIST) == engine._QB_SETTLED
    assert set(engine._QB_TRANSIENT_LIST) == engine._QB_TRANSIENT


@pytest.mark.parametrize("state,is_dl", [
    ("downloading", True), ("forcedDL", True), ("stalledDL", True), ("metaDL", True),
    ("uploading", False), ("missingFiles", False), ("error", False), ("", False),
    ("某个未来版本的新状态", False),   # 未知状态一律不当"在下"，宁可少算不可多算
])
def test_qb_is_downloading(state, is_dl):
    assert engine.qb_is_downloading(state) is is_dl
