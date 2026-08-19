"""订阅源与番单发现：RSS 源（nyaa/mikan）+ Mikan 季度发现（剧场版/OVA）+ 共享解析。"""

# 站点标识 → 源类。**显式字典，不用装饰器注册表**：读一眼就知道全集，没有导入时序的坑，
# 也与本项目"把权衡写在原地"的风格一致。
#
# 新增一个源要改的地方，全在这里能查到：
#   ① 新建 sources/<站>.py，继承 RssSource，给出 site / TZ / _hash_of / _url_of
#   ② 在下面这张表里加一行
#   ③ pages/sources.py 的类型下拉与 core/anime.py 的『补齐』分派都从这张表取，不用再改
from sources.mikan import MikanSource, mikan_search_url   # noqa: E402
from sources.nyaa import NyaaSource, nyaa_search_url     # noqa: E402

SOURCES: dict = {"nyaa": NyaaSource, "mikan": MikanSource}

# 站点标识 → "按关键词搜该站的 RSS URL"。补齐(backfill)用。
# 与 SOURCES 分开是因为它不是每个源都必须有：将来某个源没有搜索接口，缺一行即可，
# 补齐会自动跳过它，而不是像以前那样走进一个静默 `return []` 的 else 分支。
SEARCH_URL: dict = {"nyaa": nyaa_search_url, "mikan": mikan_search_url}
