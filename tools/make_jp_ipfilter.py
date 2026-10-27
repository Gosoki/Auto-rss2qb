"""手动生成 qBittorrent 用的「屏蔽某国 IP」过滤规则文件（.p2p），默认日本。

生成逻辑与取舍说明都在 core/ipfilter.py —— 程序里按设置定期自动更新的也是同一份代码，
这里只是命令行入口（qB 不跟本程序连着、或想先看一眼生成结果时用）。

用法（在项目根目录）：
    .venv/bin/python tools/make_jp_ipfilter.py              # 日本 → tools/ipfilter-jp.p2p
    .venv/bin/python tools/make_jp_ipfilter.py KR HK        # 别的国家/地区 → tools/ipfilter-kr-hk.p2p
    .venv/bin/python tools/make_jp_ipfilter.py --force      # 有源取不到也照写（默认拒写）
    .venv/bin/python tools/make_jp_ipfilter.py --no-p2p     # 不并通用反 P2P 名单（默认并）

生成完在 qB 里点一下『应用』重新载入。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import ipfilter  # noqa: E402


def main() -> int:
    force = "--force" in sys.argv
    ccs = [a.upper() for a in sys.argv[1:] if not a.startswith("-")] or ["JP"]
    out = Path(__file__).resolve().parent / f"ipfilter-{'-'.join(c.lower() for c in ccs)}.p2p"
    print(f"拉取数据源（{','.join(ccs)}）…")
    res = asyncio.run(ipfilter.generate(ccs, antip2p="--no-p2p" not in sys.argv))
    for key, n in sorted(res["counts"].items()):
        print(f"  ✓ {key:<16} {n:>6} 段")
    if res["failed"]:
        print(f"\n  ✗ 没取到：{'、'.join(res['failed'])}")
        if not force:
            print("   少一个源就少一片覆盖，而你不会知道少的是哪片 —— 已【拒绝写出文件】，"
                  "旧文件原样保留。\n   网络问题就重跑；确实要将就，加 --force。")
            return 1
    empty = [c for c in ccs if not res["per"].get(c)]
    if empty:
        print(f"❌ {','.join(empty)} 一条 IPv4 规则都没生成出来（国家代码写错了？），不写文件")
        return 1
    out.write_text(res["text"], encoding="utf-8")
    print(f"\n✅ 已写出 {out}")
    print(f"   IPv4 {res['v4']:>5} 条区间，IPv6 {res['v6']:>5} 条区间")
    print(f"   qB 载入后日志会报 'IP filter successfully parsed: N rules'，N 应约等于 {res['v4'] + res['v6']}")
    if force and res["failed"]:
        print("   ⚠️ 是在缺源的情况下生成的，覆盖面不完整")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
