"""订阅源基类与标准条目。

**本项目没有插件系统**——这里就是几个普通模块共用的基类，不是什么契约或注册机制。

加一个站要做的事（README 的『加一个订阅源要做什么』有同一份说明）：
  ① 继承 RssSource，给出 site / TZ / _hash_of / _url_of 四样；
  ② 在 sources/__init__.py 的 SOURCES 表里加一行。
其余全部逻辑在 RssSource 里，只此一份。主流程只认 ParsedItem。
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import feedparser
import httpx

from sources.parse import (candidate_names, clip_title, estimate_premiere, extract_episode_abs,
                           is_batch, kw_match, parse_multibracket, parse_title, quarter_of)

log = logging.getLogger("autorss")

# 40 位小写 hex：跨源去重键的形状。预编译在模块级（每条种子都要过一遍）。
_HEX40_RE = re.compile(r"[0-9a-f]{40}")


@dataclass
class ParsedItem:
    info_hash: str          # 40位hex小写，跨源去重键
    raw_title: str
    anime_title: str
    season: int
    episode: float          # 支持 .5；-1特别篇 -2未知
    quarter: str
    release_time: datetime | None
    download_url: str       # 直接可下载 .torrent 的地址
    source: str             # 展示用来源名，如 'ANI'
    site: str               # 下载站点，如 'nyaa'
    policy: str = "auto"      # 组策略：'auto' 全下 / 'review' 需人工确认
                              # （与 SourceGroup.policy 同名同义——这里曾叫 source_kind，
                              #   两个名字指同一件事，读代码时得来回对一次才敢确认）
    priority: int = 0          # 组优先级（越大越优先）
    episode_abs: int | None = None  # 标题写成 '16(88)' 时的绝对集号；用来推该番的跨源集号偏移
    search_names: list[str] = field(default_factory=list)  # 候选名（搜 bgm 用）


class Source:
    name = "base"

    async def fetch(self) -> list[ParsedItem]:
        raise NotImplementedError


class RssSource(Source):
    """从一条 RSS feed 抓种子的源。nyaa 与 mikan 共用它。

    【为什么要有这个基类】不是为了"插件契约"——本项目没有插件系统，这两个源就是两个普通模块。
    合并的唯一理由是：它们的 _parse 曾经是两份逐字相同的拷贝，而那份重复【已经在产生缺陷】：
    审计记录里源层的改动有 9 次是"同一件事改两遍"，其中最近一次（字幕组白名单改成大小写不敏感）
    只改了一半——用户填 lolihouse 而站上写 LoliHouse 时，那个源组每轮全灭，
    日志只有一行"0 条"，没有任何指向。

    子类只需给出四样东西：
      · site      —— 站点标识（与 sources.SOURCES 的键必须一致）
      · TZ        —— 该站 pubDate 的时区基准（不给就是 None：显示时不做换算）
      · _hash_of  —— 从 entry 取 info_hash
      · _url_of   —— 从 entry 取 .torrent 下载地址
    其余（取回、bozo 告警、40hex 校验、合集过滤、标题关键词、番名解析、多括号回退、
    字幕组白名单、发布时间、季度推算、ParsedItem 构造、异常兜底）一律在这里。

    【"只此一份"有一个例外】sources/mikan.fetch_bangumi_torrents 是第三条 RSS 解析路径，
    服务于剧场版：它不做批量过滤、不做字幕组白名单、允许 -1/-2 集号，语义与这里不同，故没有合并。
    改本类里的通用行为时，记得看一眼那边要不要同步（本项目已经因此漏过一次）。
    """

    site = ""
    # 发布时间的时区基准。RSS 的 pubDate 各站口径不同，而 feedparser 一律按 GMT 解释：
    # nyaa 的 pubDate 带 -0000（真 UTC），mikan 的不带时区（实为北京时间）。
    #
    # 【默认 None，不是 UTC】这条知识以前存在 core/engine.py 的一张 _SITE_TZ 字典里，
    # 那是"新增一个源时漏改了【不会报错】、只是发布时间整天错"的地方。搬到类属性上是为了消灭
    # 那个失效形状——可若在这里给一个 UTC 默认值，漏写的源会静静地按 UTC 显示，
    # 那个形状就原样搬了过来。默认 None 让 `_site_tz` 返回 None，`torrent_time` 走"不换算"的分支
    # （与旧字典查不到时的行为一致），而 tests/test_sources.py 有一条用例会当场红。
    TZ = None

    def __init__(self, name: str, rss_url: str, policy: str = "auto", priority: int = 0,
                 subgroups: list | None = None, title_filter: list | None = None):
        self.name = name
        self.rss_url = rss_url
        self.policy = policy
        self.priority = priority
        self.subgroups = subgroups or []        # 字幕组白名单（子串匹配组名，空=全部）
        self.title_filter = title_filter or []  # 标题关键词过滤（标题需含其一，空=不限）

    # ---- 子类覆写点 ----
    def _hash_of(self, entry) -> str:
        raise NotImplementedError

    def _url_of(self, entry) -> str:
        raise NotImplementedError

    # ---- 以下是两个源共用的全部逻辑 ----
    async def fetch(self) -> list[ParsedItem]:
        import config
        from services import fetch as _fetch
        async with httpx.AsyncClient(**config.http_client_kwargs(30)) as client:
            # 走带上限+总超时的取回：feed 地址是用户填的、内容来自第三方，
            # 裸 client.get + resp.content 既能被涓流响应永久挂住，也能被超大 body 撑爆内存
            content = await _fetch.get_bytes(client, self.rss_url)
        feed = feedparser.parse(content)
        if feed.bozo:
            # 【两个源都要告警】以前只有 nyaa 这一半有，mikan 那半静悄悄——
            # feed 结构坏掉时（站点改版、返回错误页）表现同样是"0 条"，但没人知道为什么。
            log.warning("%s Feed 解析异常（bozo），尽力处理已解析条目", self.name)
        out = []
        for entry in feed.entries:
            item = self._parse(entry)
            if item is not None:
                out.append(item)
        return out

    def _parse(self, entry) -> ParsedItem | None:
        import config
        try:
            raw_title = clip_title(entry.title)   # 第三方标题，先截长（理由见 parse.clip_title）
            info_hash = (self._hash_of(entry) or "").strip().lower()
            if not _HEX40_RE.fullmatch(info_hash):
                return None  # 必须是 40 位 hex：既能跨源去重，也防脏 hash 注入 qB 的 '|' 分隔符
            if is_batch(raw_title):
                return None  # 合集/BDRip/连续集范围 整理帖
            if self.title_filter and not any(kw_match(k, raw_title) for k in self.title_filter):
                return None  # 标题不含所需关键词（如按语言 繁日/简日 过滤）

            group, anime_title, season, episode = parse_title(raw_title)
            search_names = candidate_names(raw_title)
            if not anime_title and config.ANIME_MULTIBRACKET_PARSE:
                mb = parse_multibracket(raw_title)   # 开关开：全括号命名回退捕获番名
                if mb:
                    anime_title, search_names = mb
            if not anime_title:
                return None  # 番名解析为空（如纯多括号格式）→ 无法定位/去重，跳过免撞库
            # 白名单：子串匹配、大小写不敏感（与 title_filter 同口径，见 parse.kw_match），
            # 兼顾联合发布（如 "喵萌奶茶屋&LoliHouse"）
            if self.subgroups and not any(kw_match(g, group) for g in self.subgroups):
                return None  # 不在白名单的字幕组
            download_url = self._url_of(entry)
            if not download_url:
                # 【要记一行】合并之前 nyaa 走的是 entry.link 直取，缺 <link> 会 AttributeError
                # 进兜底、记一条 ERROR；改成 .get() 之后变成完全静默丢弃。
                # 站点改版把下载地址挪走时，表现会是"这个源突然只收到一半条目"而日志一个字都没有。
                log.warning("%s 条目缺下载地址，跳过：%s", self.name, raw_title[:60])
                return None

            # 用 feedparser 已解析的 published_parsed（C 层解析、与进程 LC_TIME 无关）。
            # 曾用 datetime.strptime(含 %a/%b 英文缩写)：非英文 locale(如 ja_JP.UTF-8)下会
            # ValueError → 丢 release_time，退化 bgm 季度识别、丢 quarter。
            release_time = None
            pp = entry.get("published_parsed")
            if pp:
                release_time = datetime(*pp[:6])
            quarter = ""
            if release_time is not None:
                quarter = quarter_of(estimate_premiere(release_time, episode, season))

            return ParsedItem(
                info_hash=info_hash,
                raw_title=raw_title,
                anime_title=anime_title,
                season=season,
                episode=episode,
                quarter=quarter,
                release_time=release_time,
                download_url=download_url,
                source=(group or self.name),
                site=self.site,
                policy=self.policy,
                priority=self.priority,
                search_names=search_names,
                episode_abs=extract_episode_abs(raw_title),
            )
        except Exception as e:
            # 【兜底代码自己绝不能再抛】这里曾经直接写 entry.get("title", "?")：
            # 条目畸形到连 .get 都没有时，异常处理器自己炸掉，异常逃出 _parse → 逃出 fetch →
            # 掀翻整轮采集。而这个 except 存在的全部意义就是"一条坏条目不该连累其余"。
            try:
                what = str(entry.get("title", "?"))[:80]
            except Exception:
                what = repr(entry)[:80]
            log.error("%s 解析条目失败: %s: %s - %s", self.name, type(e).__name__, e, what)
            return None
