"""仓库根 conftest：只做一件事——注册 NiceGUI 的 User 测试插件。

【为什么必须放在根目录】pytest 不允许在非顶层 conftest 里写 `pytest_plugins`。
用 `user_plugin` 而不是 `nicegui.testing.plugin`：后者会 import selenium（那是给真浏览器的
Screen 夹具用的），而本套用例的原则是零外部依赖、零网络。
"""
pytest_plugins = ["nicegui.testing.user_plugin"]
