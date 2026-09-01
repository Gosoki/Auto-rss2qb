"""手动下载（core/manual.py）：它是三条交付路径里最容易被漏掉的那一条。

番剧走 anime.download_anime_torrent、剧场版走 movies 的对应函数，两者都经 engine.add_to_qb；
手动这条历史上是直连 qb.add_*，于是 add_to_qb 里的两条策略（同机预建目录、hash 幂等兜底）
它一条都没有——本项目的第一种缺陷形状：同一件事有三处，只改了两处。
"""
# ---------------- 手动下载必须走 engine.add_to_qb（R17） ----------------

async def test_manual_creates_the_local_dir_like_the_other_two_paths(monkeypatch, tmp_path, cfg):
    """(R17) 手动下载是全项目【唯一】绕过 engine.add_to_qb 的交付路径，因此丢了本地预建目录。

    而它的默认保存位置恰恰是『工作目录/Temp』——一个谁也不会预先去建的目录。
    番剧与剧场版两条线都走 add_to_qb，只有这条没走：本项目的第一种缺陷形状（三处只改了两处）。
    """
    from core import engine, manual as MAN

    target = tmp_path / "Temp"
    monkeypatch.setattr(engine, "qb_is_local", lambda: True)

    async def fake_add_torrent(data, save_path, category, tags):
        return True
    monkeypatch.setattr(engine.qb, "add_torrent", fake_add_torrent)
    cfg(QB_ENABLED=True)

    res = await MAN.add_manual("", b"d4:infod6:lengthi1e4:name1:a12:piece lengthi1eee", str(target))
    assert res["ok"], res
    assert target.is_dir(), "qB 同机时没有预建保存目录"


async def test_manual_duplicate_submit_is_not_reported_as_a_failure(monkeypatch, tmp_path, cfg):
    """(R17) 同一条磁力点第二次，该说"它已经在下了"，而不是红色的『qB 未接受』。

    幂等兜底住在 engine.add_to_qb 里；绕过它的手动路径拿不到这层。
    """
    from core import engine, manual as MAN

    h = "a" * 40
    monkeypatch.setattr(engine, "qb_is_local", lambda: False)

    calls = []

    async def fake_add_url(url, save_path, category, tags):
        calls.append(url)
        return False                       # qB：这个 hash 已经在了
    async def fake_info(hashes):
        return {h: {"state": "downloading"}}
    async def boom(*a, **k):
        raise AssertionError("磁力链走到了 add_torrent —— add_to_qb 的 magnet 分支没了")
    monkeypatch.setattr(engine.qb, "add_url", fake_add_url)
    monkeypatch.setattr(engine.qb, "add_torrent", boom)   # 【必须打这个桩】
    monkeypatch.setattr(engine.qb, "torrents_info", fake_info)
    cfg(QB_ENABLED=True)

    res = await MAN.add_manual(f"magnet:?xt=urn:btih:{h}", None, str(tmp_path))
    # 【断言真的走了 add_url】不加这两句的话，把 add_to_qb 的 magnet 分支整个删掉，
    # 磁力会把 data=None 递给未打桩的真 add_torrent（无 qB 时返回 None），
    # 再被夹具自己提供的 torrents_info 经幂等兜底升级成 True —— 断言照样绿。
    # 那样测的就只是"兜底能把结果变成 True"，而那个 True 是夹具自己喂进去的。
    assert calls == [f"magnet:?xt=urn:btih:{h}"], f"没有把磁力链交给 add_url：{calls}"
    assert res["ok"] is True, f"重复提交被报成了失败：{res}"
    assert res["error"] is None


async def test_manual_offline_qb_is_still_reported_as_temporary(monkeypatch, tmp_path, cfg):
    """连不上 qB 仍要说"稍后再点一次"，不能因为走了 add_to_qb 就把 None 压成 False。"""
    from core import engine, manual as MAN

    monkeypatch.setattr(engine, "qb_is_local", lambda: False)

    calls = []

    async def fake_add_url(url, save_path, category, tags):
        calls.append(url)
        return None
    async def fake_info(hashes):
        return None
    async def boom(*a, **k):
        raise AssertionError("磁力链走到了 add_torrent")
    monkeypatch.setattr(engine.qb, "add_url", fake_add_url)
    monkeypatch.setattr(engine.qb, "add_torrent", boom)
    monkeypatch.setattr(engine.qb, "torrents_info", fake_info)
    cfg(QB_ENABLED=True)

    res = await MAN.add_manual("magnet:?xt=urn:btih:" + "b" * 40, None, str(tmp_path))
    assert calls, "没有把磁力链交给 add_url"
    assert res["ok"] is False
    assert "连不上 qB" in (res["error"] or ""), res


async def test_manual_does_not_chmod_an_existing_directory(monkeypatch, tmp_path, cfg):
    """(R18) 手动下载不得把用户【已有】的目录改成 0777。

    add_to_qb 里那句 chmod 的用途是跨用户 qB 写得进去，对象本该是 build_save_path 生成的、
    位于下载根之下的叶子目录。而手动下载的保存位置是用户在输入框里自由填的，
    完全可能指向一个已经存在的媒体库目录 —— R17 让手动路径统一走 add_to_qb 时把它一起带了过去。
    """
    import os

    from core import engine, manual as MAN

    lib = tmp_path / "media-library"
    lib.mkdir(mode=0o755)
    os.chmod(lib, 0o755)
    before = os.stat(lib).st_mode & 0o777
    monkeypatch.setattr(engine, "qb_is_local", lambda: True)

    async def ok(*a, **k):
        return True
    monkeypatch.setattr(engine.qb, "add_url", ok)
    cfg(QB_ENABLED=True)

    res = await MAN.add_manual("magnet:?xt=urn:btih:" + "c" * 40, None, str(lib))
    assert res["ok"], res
    assert os.stat(lib).st_mode & 0o777 == before, "用户已有的目录被 chmod 成了 0777"


async def test_manual_still_creates_and_opens_up_a_new_leaf_dir(monkeypatch, tmp_path, cfg):
    """反向：新建出来的叶子目录仍然要 0777——跨用户 qB 靠的就是它。"""
    import os

    from core import engine, manual as MAN

    leaf = tmp_path / "26C" / "某番"
    monkeypatch.setattr(engine, "qb_is_local", lambda: True)

    async def ok(*a, **k):
        return True
    monkeypatch.setattr(engine.qb, "add_url", ok)
    cfg(QB_ENABLED=True)

    await MAN.add_manual("magnet:?xt=urn:btih:" + "d" * 40, None, str(leaf))
    assert leaf.is_dir()
    assert os.stat(leaf).st_mode & 0o777 == 0o777, "新建的叶子目录没有放开权限"
