"""(r41) 提示文案：wrb 的"先标再删"（W1/W2）、rer 在锁源时不再说"照常自动下"（R1）、mva 项带两边的 id。"""
from datetime import datetime, timedelta

import pytest

from core import alerts
from db.models import Anime, AnimeTorrent, Movie, MovieTorrent

W1 = ("先标『不属于本季』（或重绑）；标完到详情页点『看清单并清理』删文件，就不会影响本季。"
      "没标就删的话，删时选了『这一集不要了』（或整部番删文件），本季这一集就会被挡住。")


def _wrb(db):
    with db.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=3, quarter="26C", confirmed=True, rejected=False,
                  bangumi_id=598058, total_episodes=12, air_date="2026-07-05")
        s.add(a); s.commit(); s.refresh(a)
        for i, ep in enumerate((1, 2, 3)):
            s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{i:040x}", raw_title=f"[组] - {ep}", episode=float(ep),
                               status="sent", release_time=datetime(2024, 1, 1)))
        s.commit()
        return a.id


def test_w1_wrb_alert_says_mark_then_clean(clean_tables):
    _wrb(clean_tables)
    f = [x for x in alerts.scan() if x["kind"] == "wrb"]
    assert len(f) == 1 and W1 in f[0]["text"], f
    assert "别删" not in f[0]["text"]


async def test_w2_backlog_push_uses_same_sentence(clean_tables, cfg, monkeypatch):
    from core import anime as A
    from services import notify
    sent = []

    async def fake_event(kind, text, key=None, cooldown=0):
        sent.append(text)
        return True
    monkeypatch.setattr(A, "notify_event", fake_event)
    cfg(NOTIFY_BACKLOG_MIN=999)
    notify._last_sent.clear()
    A._wb_notified = set()
    _wrb(clean_tables)
    await A.sweep_alerts()
    msg = [t for t in sent if "绑定看着不对" in t]
    assert msg and W1 in msg[0] and "别删" not in msg[0], sent


def _rer(db, pref):
    t0 = datetime(2026, 3, 1)
    with db.get_session() as s:
        a = Anime(title="x", display_name="相反", season=2, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=13, bangumi_id=1, pref_source=pref)
        s.add(a); s.commit(); s.refresh(a)
        n = 0
        for ep in (6, 7, 8):
            for src, st, rt in (("喵萌", "sent", t0 + timedelta(weeks=ep)),
                                ("绿茶", "pending", t0 + timedelta(weeks=ep + 22))):
                n += 1
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{n:040x}", raw_title=f"[{src}] x [{ep:02d}]",
                                   episode=ep, status=st, source=src, release_time=rt))
        s.commit()


@pytest.mark.parametrize("pref", ["喵萌", None], ids=["锁源", "没锁"])
def test_r1_rer_text_depends_on_lock(clean_tables, cfg, pref):
    cfg(ANIME_RERELEASE_WEEKS=20, ANIME_FINISH_UNSUB=False)
    _rer(clean_tables, pref)
    f = [x for x in alerts.scan() if x["kind"] == "rer"]
    assert len(f) == 1, f
    r1 = "这部番锁定了『喵萌』，标完后新来的『绿茶』第 6～8 集不会自动下，标记时可以顺带下载。"
    if pref:
        assert r1 in f[0]["text"] and "标完这几集照常自动下" not in f[0]["text"], f[0]["text"]
        # R1 之后紧跟『这个按钮』会被读成『顺带下载』，按钮要点名
        assert "『不属于本季』是那一行右边的『非本季』按钮；旧的那几条右边没有『非本季』的，就在新来的那几条上逐条点『下载』。" in f[0]["text"]
        assert "这个按钮" not in f[0]["text"]
    else:
        assert "标完这几集照常自动下" in f[0]["text"] and "锁定了" not in f[0]["text"], f[0]["text"]


def test_r1_lock_group_also_has_new_copies_keeps_old_text(clean_tables, cfg):
    """新来的里锁定组自己也有：标完它会自动下，照旧那句。"""
    cfg(ANIME_RERELEASE_WEEKS=20, ANIME_FINISH_UNSUB=False)
    _rer(clean_tables, "绿茶")
    f = [x for x in alerts.scan() if x["kind"] == "rer"][0]
    assert "标完这几集照常自动下" in f["text"] and "锁定了" not in f["text"]


def test_mva_item_carries_both_ids(clean_tables):
    with clean_tables.get_session() as s:
        a = Anime(title="剧场版 X", display_name="剧场版 X", season=1, quarter="26A", confirmed=False,
                  rejected=True, bangumi_id=583746)
        m = Movie(title="X 剧场版", display_name="X 剧场版", quarter="2026", bangumi_id=583746)
        s.add(a); s.add(m); s.commit(); s.refresh(a); s.refresh(m)
        s.add(MovieTorrent(movie_id=m.id, info_hash="7" * 40, raw_title="mv", status="pending"))
        s.commit()
        aid, mid = a.id, m.id
    f = [x for x in alerts.scan() if x["kind"] == "mva"][0]
    assert (f["anime_id"], f["movie_id"], f["a_name"], f["m_name"]) == (aid, mid, "剧场版 X", "X 剧场版")
    assert f["fact"] == "bgm=583746;ast=01", "两边的 id 只给按钮用，不进事实指纹"
