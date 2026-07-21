"""核心逻辑：共用底层 engine + TV 番剧 anime + 剧场版 movies + 后台协程 worker。

依赖根级的基础层（config / db / models）与 services / sources；main、pages 反过来用这里。"""
