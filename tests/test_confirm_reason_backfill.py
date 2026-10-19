"""给『原因这一列上线前就进了待确认』的番推断原因（core.anime.backfill_confirm_reasons）。

只认两种有把握的：绑定看着不对 → 疑似绑错季；一集没下过、种子只可能来自人工审核源 → 人工审核源发现。
推断不出的保持为空（显示『未记录』），宁可不写也不写错。
"""
from datetime import datetime

from sqlmodel import select

from core import anime as A
from db.models import Anime, AnimeTorrent, SourceGroup


def _setup(s):
    # clean_tables 不清源组表：别的用例（seed_source_groups）留下的同名组会撞唯一约束，先清掉
    for g in s.exec(select(SourceGroup)):
        s.delete(g)
    s.commit()
    s.add(SourceGroup(name="ANi", site="nyaa", feed="u=ANi", policy="auto", priority=100))
    s.add(SourceGroup(name="Mikan(兜底)", site="mikan", feed="classic", policy="auto", priority=20,
                      subgroups="ANi,喵萌奶茶屋"))
    s.add(SourceGroup(name="Mikan(后备)", site="mikan", feed="classic2", policy="review", priority=10,
                      subgroups="桜都字幕组,Nix-Raws"))
    s.commit()


def _anime(s, title, rows, **kw):
    a = Anime(title=title, display_name=title, season=1, quarter="26D", confirmed=False,
              bangumi_id=kw.pop("bangumi_id"), **kw)
    s.add(a); s.commit(); s.refresh(a)
    for i, (src, site, ep, status) in enumerate(rows):
        s.add(AnimeTorrent(anime_id=a.id, info_hash=f"{a.id:08x}{i:032x}", raw_title=f"[{src}] {title} - {ep}",
                           source=src, site=site, episode=ep, status=status, created_at=datetime.now()))
    s.commit()
    return a.id


def test_infers_review_and_wrong_season_and_leaves_the_rest(clean_tables):
    with clean_tables.get_session() as s:
        _setup(s)
        review = _anime(s, "审核源番", [("桜都字幕组", "mikan", 1, "pending")], bangumi_id=1)
        # 绑到了只有 1 集的条目，却收到多个不同正集号 —— binding_looks_wrong 的第二条判据
        wrong = _anime(s, "绑错番", [("ANi", "nyaa", 1, "pending"), ("ANi", "nyaa", 2, "pending")],
                       bangumi_id=2, total_episodes=1)
        auto = _anime(s, "自动源番", [("ANi", "nyaa", 1, "pending")], bangumi_id=3)
        # 下过集：确认过又被打回来的（补齐/绑定），不是审核源新发现的
        handled = _anime(s, "下过的番", [("桜都字幕组", "mikan", 1, "sent")], bangumi_id=4)
        # 同一个组也被自动源收（Mikan(兜底) 白名单含 ANi）：说不清是哪个源组发现的
        both = _anime(s, "两边都收", [("ANi", "mikan", 1, "pending")], bangumi_id=5)
        kept = _anime(s, "已有原因", [("桜都字幕组", "mikan", 1, "pending")], bangumi_id=6,
                      confirm_reason="backfill")

    assert A.backfill_confirm_reasons() == 2
    assert A.backfill_confirm_reasons() == 0, "重复跑应当什么都不改"
    with clean_tables.get_session() as s:
        got = {aid: s.get(Anime, aid).confirm_reason for aid in (review, wrong, auto, handled, both, kept)}
    assert got == {review: "review", wrong: "wrong_season", auto: None, handled: None, both: None,
                   kept: "backfill"}, got
