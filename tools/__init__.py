"""wows_replay 的辅助脚本集合。

这里的脚本可以脱离 bot 单独运行：
python fetch_renderer_data.py / bash setup_toolkit.sh。
做成包只是为了 updater.py 能 import 它们的函数，不会被 nonebot 当插件加载
（load_plugins 用 pkgutil.iter_modules，不递归进包内部）。
"""
