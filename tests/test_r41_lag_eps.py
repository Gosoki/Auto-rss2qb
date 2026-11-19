"""锁定源落后提醒的集数门槛（ANIME_LOCK_LAG_EPS，默认 2）：只落后一集多半是字幕组还在做，推送和仪表盘都不报；
落后两集起才报。详情页横幅用的 locked_out_episodes 不受影响，落后一集照样列出来。零网络：推送打桩。"""
from datetime import datetime, timedelta

import pytest

from core import anime as A
from db.models import Anime, AnimeTorrent

LOCK = "喵萌奶茶屋"


def _seed(db, other_max):
    now = datetime.now()
    with db.get_session() as s:
        a = Anime(title="某番", display_name="某番", season=1, quarter="26C", confirmed=True, rejected=False,
                  total_episodes=12, bangumi_id=1, pref_source=LOCK)
        s.add(a)
        s.commit()
        s.refresh(a)
        n = 0
        for src, top in ((LOCK, 5), ("ANi", other_max)):
            for e in range(1, top + 1):
                at = now - timedelta(days=80 - 7 * e)
                n += 1
                s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{n:040x}", source=src, site="nyaa", anime_title="某番",
                                   season=1, episode=float(e), status="sent" if src == LOCK else "skipped",
                                   raw_title=f"[{src}] 某番 - {e:02d}", download_url=f"https://x/{n}.torrent",
                                   created_at=at, release_time=at))
        s.commit()
        return a.id


@pytest.mark.parametrize("other_max, need, alert", [
    (6, 2, False),   # 落后一集：不报
    (7, 2, True),    # 落后两集：报
    (6, 1, True),    # 门槛调回 1：落后一集也报
])
async def test_lag_alert_needs_enough_episodes(clean_tables, cfg, monkeypatch, other_max, need, alert):
    cfg(ANIME_LOCK_LAG_DAYS=7, ANIME_LOCK_SYNC_DAYS=2, ANIME_LOCK_FALLBACK_DAYS=0, ANIME_LOCK_LAG_EPS=need,
        ALERT_REPEAT_DAYS=7)
    aid = _seed(clean_tables, other_max)
    with clean_tables.get_session() as s:
        animes = [s.get(Anime, aid)]
    assert A.locked_out_episodes(animes)[aid]["eps"] == list(range(6, other_max + 1))   # 横幅口径不变
    assert bool(A.suspect_lock_lag()) is alert
    msgs = []

    async def fake_notify(kind, msg, **kw):
        msgs.append(msg)
        return True
    monkeypatch.setattr(A, "notify_enabled", lambda k: True)
    monkeypatch.setattr(A, "notify_event", fake_notify)
    assert (await A.sweep_lock_lag() == 1) is alert and bool(msgs) is alert
