"""三类『只报不改』的巡检发现，以及用户对它们点的『知道了』。

【这个模块为什么存在】`core/anime.py` 里的三个 `suspect_*` 只回答"现在有哪些发现"，
而横幅文案长在 `pages/anime.py`、推送长在 `sweep_alerts`。要让"已读"同时在这三处生效，
就得有一个地方同时管住【判据 → 文案 → 已读过滤】。散在三处的话，"同一个决定只落一处"
（本项目第①号缺陷形状）是必然的：R35 的评审已经实测出一例 ——
已读只作用在仪表盘上时，手机每 6 小时照旧响一次。

【一条发现拆成两半】
  · `ident`（身份）—— 这是"哪一条发现"。变了＝另一条发现，本来就该重新显示。
  · `fact` （事实指纹）—— 这是"当时是什么情况"。与此刻逐字不等就**复活**，
    并且因为它是 `k=v;k=v` 的人话串，还能逐字段讲出"你读过之后，什么变了"。

【身份里为什么要带 created_at 而不是只用 id】SQLite 的整型主键是 rowid，模型里
`id: int | None = Field(primary_key=True)` 生成的是**不带 AUTOINCREMENT** 的
INTEGER PRIMARY KEY —— 删掉最大号那行之后 id 会被回收（实测：插 1/2/3、删 3、再插，
新行拿到的就是 3）。而"删掉一条番"在番剧表里是个现成的按钮。只按 id 认身份的话，
一条**全新的、完全不同的**发现会被上一条的已读静默吃掉 —— 那是"不可见"，
本项目最不能接受的一类。

【为什么用 created_at.isoformat() 而不是 .timestamp()】两个坑，R35 的评审都在真库上复现过：
  · `created_at` 是 `datetime.now()` 产的 **naive** 值，naive 的 `.timestamp()` 按**当前时区**
    解释。同一行同一个库，UTC / Asia/Shanghai / America/New_York 各算出一个不同的数 ——
    主机改一次时区、或迁到 TZ 不同的宿主上，数据一行没动而全部已读复活。
  · `int(...)` 截到整秒，而同秒是生产的真实形状（真库 100 部番只占 97 个不同的秒），
    于是"同秒删除+重建"会撞出逐字相同的身份。
  isoformat 带微秒、且就是库里存的那个字面量，两个坑都不沾。

【事实指纹里为什么全是原始位、没有展示串】`a_state` 那种三态串（追番中/人工拒绝/超期忽略·待确认）
**不是单射**：`(confirmed=0, rejected=0)` 与 `(0,1)` 都映成"超期忽略/待确认"，
而横幅尾巴那句「其中『X』已被忽略，它下面的集不会下载」只读 `rejected`。
用展示串当指纹，"那部番从待确认掉成超期忽略"（它下面的集从此一集都不会被下）这件真事
一个字都不动。所以指纹一律吃 `(confirmed, rejected)` 的原始两位。
`confirmed` 尤其不能漏：它在入库链路上是**自动**升的（见 core/anime 的自动升确认分支），
一对"待确认+待确认"的重复番被读过之后会被后台各自升成追番中 —— 那正是危险变真的一刻。
"""
import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from core import anime as _anime
from db import get_session
from db.models import AlertAck

log = logging.getLogger("autorss")

# 三类的键与人话名。加一类要同时给出 _IDENT/_FACT/_TEXT 三个构造式，
# 下面 `scan()` 的断言会逼着你补齐。
KINDS = {"dup": "同一部番被拆成两条", "mva": "番剧与剧场版重号", "wrb": "绑定看着不对"}


def _tag(row_id, created_at) -> str:
    """一行的身份原子：`id@出生时刻`。理由见模块 docstring。"""
    return f"{row_id}@{created_at.isoformat() if created_at else '-'}"


def _bits(confirmed, rejected) -> str:
    """订阅态的**原始两位**。别换成展示串，理由见模块 docstring。"""
    return f"{int(bool(confirmed))}{int(bool(rejected))}"


def _eps_str(eps) -> str:
    """集号集合 → `1,2,3` 的排序串。

    【有意不做区间压缩】压缩（`1-8,12`）更短，但那是自己写的编码、要自己证单射；
    而这里的值只有几十个数，逗号串既单射又能直接念给用户听。
    """
    return ",".join(str(int(e)) for e in sorted(eps))


# ── 三类的 身份 / 事实 / 文案 ──────────────────────────────────────────────
# 文案放这里（而不是留在 pages/anime.py）是因为它要被用在三个地方：横幅、已读列表里的存档、
# 以及将来任何一处想说"这条发现是什么"的地方。留在页面里的话，已读列表就得抄第二遍。

def _dup(d: dict) -> dict:
    tail = ("；其中『%s』已被忽略，它下面的集不会下载"
            % (d["a_name"] if d["a_rejected"] else d["b_name"])
            if (d["a_rejected"] or d["b_rejected"]) else "")
    return {
        "kind": "dup",
        "ident": f"dup:{_tag(d['a'], d['a_born'])}+{_tag(d['b'], d['b_born'])}",
        # bgm：任一条改绑 ⇒「这对到底是不是同一部番」的结论作废（改绑正是横幅给的指引）
        # 订阅位：横幅尾巴那句话的全部依据，也是"两条都变追番中"这种新危险的唯一信号
        "fact": (f"abgm={d['a_bgm']};bbgm={d['b_bgm']};"
                 f"ast={_bits(d['a_confirmed'], d['a_rejected'])};"
                 f"bst={_bits(d['b_confirmed'], d['b_rejected'])}"),
        "text": (f"『{d['a_name']}』(#{d['a']}，bgm {d['a_bgm']}) 与 "
                 f"『{d['b_name']}』(#{d['b']}，bgm {d['b_bgm']}) 共用番名 "
                 f"「{'、'.join(d['shared'])}」，多半是同一部番被拆成了两条{tail}。"
                 "去详情页核对 bgm 绑定：把错的那条改绑成对的 bgm，身份守卫会自动把它们合并。"),
    }


def _mva(m: dict) -> dict:
    return {
        "kind": "mva",
        "ident": f"mva:{_tag(m['a'], m['a_born'])}+{_tag(m['m'], m['m_born'])}",
        # 【an/mn 有意不进指纹】它们随采集单调增长（真库 mn=3 与 23，每来一个新版本就变），
        # 进了指纹就是每采到一条种子让用户重新点一次『知道了』—— 用户点名说这不要。
        # 【ast 必须进】番剧那条从"超期忽略"变成"追番中"正是危险从纸面变成真的那一刻：
        # 两边会交付同一个 info_hash，qB 只收一次，两条记录都显示已交付而文件只落一个目录。
        "fact": f"bgm={m['bgm']};ast={_bits(m['a_confirmed'], m['a_rejected'])}",
        "text": (f"『{m['a_name']}』(bgm {m['bgm']}) 在番剧表(#{m['a']}，{m['a_state']}，"
                 f"{m['an']} 条种子)和剧场版(#{m['m']}，{m['mn']} 条)里**各有一条记录**。"
                 "剧场版那条通常才是对的。别去点『恢复订阅』——两边会交付同一个种子，"
                 "而 qB 只收一次：两条记录都会显示『已交付』，文件却只落在其中一个目录。"
                 "去番剧表把这一条删掉，或确认它确实是电视版。"),
    }


def _wrb(w: dict) -> dict:
    eps = "、".join(str(e) for e in w["eps"])
    tot = f"共 {w['total']} 集" if w["total"] else "集数未知"
    return {
        "kind": "wrb",
        "ident": f"wrb:{_tag(w['id'], w['born'])}",
        # 【进指纹的是"触发集号的集合"，不是"坏种子条数"】同一集又来一个字幕组的版本
        # （条数 +1、集合不变）是常态噪声；出现一个**新的**坏集号才是新事实。
        # 【trigger_eps 覆盖两条判据】`_binding_looks_wrong_rows` 有两条：① 集号不可能属于这一季；
        # ② total_episodes==1 却收到多个正片集号。判据②命中时坏集号恒为空 ——
        # 光用坏集号的话，那一整类的指纹里没有任何随事实变化的量，**永远不会复活**。
        "fact": (f"bgm={w['bgm']};season={w['season']};"
                 f"eps={_eps_str(w['trigger_eps'])}"),
        "text": (f"『{w['name']}』(#{w['id']}，bgm {w['bgm']}，第 {w['season']} 季·{tot})"
                 f" 下面有 {w['bad']} 条种子的集号不可能属于所绑的这一季"
                 + (f"（如第 {eps} 集）" if eps else "")
                 + "——多半是 bgm 绑错了季。去详情页核对绑定；"
                 "或者那批种子本就属于别的季，删掉它们即可。"
                 "在这之前，它们会一直占着集去重、挡住真正的本季集。"),
    }


_BUILD = {"dup": (_anime.suspect_duplicate_anime, _dup),
          "mva": (_anime.suspect_movie_as_anime, _mva),
          "wrb": (_anime.suspect_wrong_binding, _wrb)}


def scan() -> list[dict]:
    """跑三类判据，返回统一形状的发现列表（**不**过滤已读）。只读。

    每条：{kind, ident, fact, text}。顺序固定（dup → mva → wrb），与横幅原来的顺序一致。
    """
    assert set(_BUILD) == set(KINDS), "加了一类却没给全 判据/构造式/人话名"
    out = []
    for kind in ("dup", "mva", "wrb"):
        find, build = _BUILD[kind]
        for raw in find():
            out.append(build(raw))
    return out


def ident_of_wrb(w: dict) -> str:
    """一条『绑定看着不对』的身份。给 `anime.sweep_alerts` 的推送过滤用 —— 它只有这一类。"""
    return _wrb(w)["ident"]


def fact_of_wrb(w: dict) -> str:
    """同上，事实指纹。"""
    return _wrb(w)["fact"]


def is_acked(ident: str, fact: str) -> bool:
    """这一条是不是"读过了、而且事实没变"。复活（事实变了）返回 False —— 该重新说一次。"""
    with get_session() as s:
        row = s.exec(select(AlertAck).where(AlertAck.ident == ident)).first()
    return row is not None and row.fact == fact


def _acks() -> dict:
    with get_session() as s:
        return {r.ident: r for r in s.exec(select(AlertAck))}


def view() -> dict:
    """仪表盘要的全部东西：{live, acked}。

    · `live`  —— 此刻要显示的发现。已读且事实没变的不在里面；事实变了的**在**里面，
                 并带上 `revived`（逐字段的"你读过之后变了什么"）。
    · `acked` —— 已读记录 + 它此刻还成不成立（`state`: fresh / gone），给『已读的提示』列表用。

    【这里一个字都不写库】它挂在仪表盘的同步构建路径上（每次渲染 + 每 30 秒各一遍）。
    在渲染路径上写库是本项目明令不做的事；而且 30 秒一次的清理会把用户正在看的那一行
    删在他眼皮底下。已读记录的清理只由用户在列表里手动做。
    """
    found = scan()
    acks = _acks()
    live, seen = [], set()
    for f in found:
        seen.add(f["ident"])
        r = acks.get(f["ident"])
        if r is None:
            live.append({**f, "revived": None})
        elif r.fact == f["fact"]:
            continue                                    # 读过了、事实没变 → 收起来
        else:
            live.append({**f, "revived": {"at": r.created_at,
                                          "changed": fact_diff(r.fact, f["fact"])}})
    acked = [{"id": r.id, "kind": r.kind, "ident": r.ident, "summary": r.summary,
              "at": r.created_at, "state": "fresh" if r.ident in seen else "gone"}
             for r in sorted(acks.values(), key=lambda x: x.created_at, reverse=True)]
    return {"live": live, "acked": acked}


FACT_LABEL = {"abgm": "左边那条绑的 bgm", "bbgm": "右边那条绑的 bgm",
              "ast": "左边那条的订阅状态", "bst": "右边那条的订阅状态",
              "bgm": "绑的 bgm", "season": "季号", "eps": "出问题的集号"}
_BITS_CN = {"00": "待确认", "01": "超期忽略", "10": "追番中", "11": "人工拒绝"}


def fact_diff(old: str, new: str) -> list:
    """两串事实指纹的逐字段差 → [(人话字段名, 旧值, 新值), …]。

    指纹是 `k=v;k=v` 的人话串（值里不会出现 `;` 与 `=`），就是为了这一步能直接讲给用户听。
    """
    def _parse(x):
        return dict(p.split("=", 1) for p in x.split(";") if "=" in p)

    o, n = _parse(old), _parse(new)
    out = []
    for k, v in n.items():
        if o.get(k) == v:
            continue
        cn = _BITS_CN.get if k in ("ast", "bst") else (lambda x, d=None: d)
        out.append((FACT_LABEL.get(k, k),
                    cn(o.get(k), o.get(k, "（当时没有这一项）")) or o.get(k, "（当时没有这一项）"),
                    cn(v, v) or v))
    return out


def ack(ident: str, kind: str, fact: str, summary: str) -> None:
    """记下"这一条我读过了"。重复点收敛成一条（upsert）。

    并发：两个标签页同时点同一条会撞 `uq_alert_ack_ident`，捕获后回滚重读一次即可
    （`config.load_from_db` 里有同款先例）。
    """
    with get_session() as s:
        row = s.exec(select(AlertAck).where(AlertAck.ident == ident)).first()
        if row is None:
            row = AlertAck(ident=ident, kind=kind, fact=fact, summary=summary)
        else:
            row.kind, row.fact, row.summary = kind, fact, summary
            row.created_at = datetime.now()
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()            # 另一个标签页刚插进去；它写的和我们要写的是同一条，就此收手
            log.info("这条告警已读记录刚被另一处写下了，不重复写 - %.60s", ident)


def unack(ident: str) -> bool:
    """取消已读：这条发现立刻回到仪表盘。返回是否真的删掉了一行。"""
    with get_session() as s:
        row = s.exec(select(AlertAck).where(AlertAck.ident == ident)).first()
        if row is None:
            return False
        s.delete(row)
        s.commit()
    return True


def drop_gone() -> int:
    """清掉"已经不成立了"的已读记录（判据不再命中的那些）。返回清掉几条。

    【只由用户手动触发，不做自动清理】自动删会撞两件事：① 判据是每 30 秒跑一遍的，
    而巡检与页面之间隔着真实的网络往返，"此刻扫不到"不等于"它不存在了"；
    ② 用户刚点下的『知道了』可能在他还没松手时就被后台删掉，只留一个绿色 toast。
    """
    alive = {f["ident"] for f in scan()}
    with get_session() as s:
        rows = [r for r in s.exec(select(AlertAck)) if r.ident not in alive]
        for r in rows:
            s.delete(r)
        if rows:
            s.commit()
    return len(rows)
