"""enrich_anime 的『两条番绑到同一个 bgm』通知要在数据库会话关掉之后再发。

推送最长要等 NOTIFY_TIMEOUT；会话跨这个 await 开着，在 MySQL 上就白占一条连接。
"""
import contextlib
from datetime import datetime, timedelta

from core import anime as A
from db.models import Anime

BGM = 9102


async def test_dup_notice_is_sent_after_the_session_closes(clean_tables, monkeypatch):
    depth = {"n": 0}
    inner = A.get_session

    @contextlib.contextmanager
    def _tracked():
        with inner() as s:
            depth["n"] += 1
            try:
                yield s
            finally:
                depth["n"] -= 1
    monkeypatch.setattr(A, "get_session", _tracked)

    sent = []

    async def _notify(kind, msg, **kw):
        sent.append((kind, msg, kw.get("key"), depth["n"]))
        return True
    monkeypatch.setattr(A, "notify_event", _notify)

    async def _resolve(names, release_time, episode, info_hash):
        return {"bangumi_id": BGM, "display_name": "同号番", "jp_name": "同号番JP",
                "air_date": (datetime.now() - timedelta(days=3)).date().isoformat(),
                "total_episodes": 12, "platform": "TV", "quarter": "26D"}
    monkeypatch.setattr(A.enrich, "resolve", _resolve)

    with clean_tables.get_session() as s:
        other = Anime(title="先来的", season=1, bangumi_id=BGM, confirmed=True)
        me = Anime(title="后来的", season=1, bangumi_id=None, confirmed=False, confirm_reason="unmatched")
        s.add(other); s.add(me); s.commit(); s.refresh(me)
        aid = me.id

    assert await A.enrich_anime(aid) is True
    assert len(sent) == 1, "同 bgm 的提醒没发"
    kind, msg, key, open_sessions = sent[0]
    assert kind == "backlog" and key == f"dup:{BGM}" and "绑到了同一个 bgm" in msg
    assert open_sessions == 0, "通知是在会话还开着时发的"
