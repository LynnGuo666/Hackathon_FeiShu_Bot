"""插件框架：功能模块以 Plugin 为单元装配、启停、注销（热插拔）。

一个 Plugin 声明：
- name：唯一名（注销/查询用）
- dependencies：依赖的其他插件名（装配时拓扑排序，保证被依赖者先 setup）
- setup()：注册指令/回调/钩子/定时任务（幂等，可重复调用）
- teardown()：注销注册 + 释放资源（默认实现调 REGISTRY.unregister_plugin）

PluginManager 维护注册表，提供 enable/disable/status；调度器引用由 main 注入，
disable 时同步从 APScheduler 摘除该插件的 job，enable 时重新挂上。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .registry import REGISTRY

if TYPE_CHECKING:
    from .registry import Registry

log = logging.getLogger(__name__)


class Plugin:
    """功能插件基类：子类覆写 name/dependencies/setup，可选覆写 teardown。

    注意：故意不用 dataclass——子类以类属性声明 name/dependencies，
    dataclass 生成的 __init__ 会用父类默认值覆盖它们。
    """

    name: str = ""
    dependencies: tuple[str, ...] = ()
    # 注册表引用：默认全局 REGISTRY；测试可注入局部 Registry
    registry: "Registry" = None

    def __init__(self, name: str = "", dependencies: tuple[str, ...] = ()) -> None:
        self.name = name or type(self).name
        self.dependencies = tuple(dependencies) or tuple(type(self).dependencies)

    @property
    def reg(self) -> "Registry":
        return self.registry or REGISTRY

    def setup(self) -> None:
        """注册指令、卡片回调、群消息钩子、定时任务。必须幂等。"""
        raise NotImplementedError

    def teardown(self) -> None:
        """默认注销本插件注册的全部能力；有额外资源（如缓存）时子类覆写并 super()。"""
        self.reg.unregister_plugin(self.name)


class PluginManager:
    """插件注册表 + 生命周期管理。"""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        self._enabled: set[str] = set()
        self._scheduler = None  # AsyncIOScheduler，由 main 注入

    def attach_scheduler(self, scheduler) -> None:
        self._scheduler = scheduler

    def register(self, *plugins: Plugin) -> None:
        """登记插件（不 setup）。依赖的插件必须先登记或随后登记（setup 前统一排序）。"""
        for p in plugins:
            if not p.name:
                raise ValueError(f"插件缺少 name: {p!r}")
            if p.name in self._plugins:
                raise ValueError(f"插件名重复: {p.name}")
            self._plugins[p.name] = p

    def _resolve_order(self, names: list[str]) -> list[str]:
        """拓扑排序：被依赖者在前。未知依赖报错。"""
        order: list[str] = []
        seen: set[str] = set()

        def visit(n: str, stack: set[str]) -> None:
            if n in seen:
                return
            if n in stack:
                raise ValueError(f"插件依赖成环: {' -> '.join(stack | {n})}")
            p = self._plugins.get(n)
            if p is None:
                raise ValueError(f"插件 {n} 依赖未登记的插件: {n}")
            stack.add(n)
            for dep in p.dependencies:
                visit(dep, stack)
            stack.discard(n)
            seen.add(n)
            order.append(n)

        for n in names:
            visit(n, set())
        return order

    def setup_all(self) -> list[str]:
        """按依赖序 setup 全部插件并启用。返回启用插件名列表。"""
        order = self._resolve_order(list(self._plugins))
        for name in order:
            self._setup_one(name)
        return list(self._enabled)

    def _setup_one(self, name: str) -> None:
        p = self._plugins[name]
        if name in self._enabled:
            return
        # 依赖必须先启用
        for dep in p.dependencies:
            if dep not in self._enabled:
                raise RuntimeError(f"插件 {name} 的依赖 {dep} 未启用")
        p.setup()
        self._enabled.add(name)
        self._reschedule_jobs(p)
        log.info("插件已启用: %s", name)

    def enable(self, name: str) -> None:
        if name not in self._plugins:
            raise KeyError(f"未登记的插件: {name}")
        order = self._resolve_order([name])
        for n in order:
            self._setup_one(n)

    def disable(self, name: str) -> None:
        p = self._plugins.get(name)
        if p is None or name not in self._enabled:
            return
        # 先停调度器里的 job，再注销注册
        self._unschedule_jobs(p)
        p.teardown()
        self._enabled.discard(name)
        log.info("插件已停用: %s", name)

    def status(self) -> list[dict]:
        return [{"name": p.name, "enabled": p.name in self._enabled,
                 "dependencies": list(p.dependencies)}
                for p in self._plugins.values()]

    # ---------- 调度器联动 ----------

    def _reschedule_jobs(self, plugin: Plugin) -> None:
        if self._scheduler is None:
            return
        for jname, interval, fn in plugin.reg.jobs:
            if plugin.reg.owners.get(jname) != plugin.name:
                continue
            if self._scheduler.get_job(jname) is None:
                self._scheduler.add_job(fn, "interval", minutes=interval,
                                        id=jname, name=jname)

    def _unschedule_jobs(self, plugin: Plugin) -> None:
        if self._scheduler is None:
            return
        for jname, _interval, _fn in plugin.reg.jobs:
            if plugin.reg.owners.get(jname) != plugin.name:
                continue
            job = self._scheduler.get_job(jname)
            if job is not None:
                job.remove()

    @property
    def owners(self) -> dict[str, str]:
        return REGISTRY.owners
