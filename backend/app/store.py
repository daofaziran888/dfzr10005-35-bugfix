"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
除了业务表，这里还提供两项审计写入需要的基础能力：
1. transaction()：写入中途失败时把业务表与审计事件链一起回滚，避免“审计写失败但业务已改”；
2. 事件链索引：同一业务记录的事件按登记顺序串联，归档后的后续变更只生成修订引用。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from typing import Any, Iterator

from app.audit_integrity import STATUS_ARCHIVED, fingerprint
from app.seed import SEED_ROWS

# 每个模块的落库失败开关，仅用于本地演练“落库失败时业务动作不能显示成功”。
_FAILING_MODULES: set[str] = set()


class StoreWriteError(RuntimeError):
    """模拟持久化层写入失败。"""


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 业务行统一带乐观版本号，跨模块对同一对象写入时用来判断基底版本。
        for rows in self._tables.values():
            for row in rows:
                row.setdefault("版本", 0)
        # 审计事件链：事件链编号 -> 按时间顺序排列的事件 id 列表。
        # 该索引只由 append 在写入成功后维护，避免业务侧读改重复登记。
        self._event_chains: dict[str, list[int]] = {}
        for row in self.rows("audit"):
            # 旧接口写入的审计记录可能没有复核人字段：这里只补“没有这个字段”，
            # 不把 None 改成当前账号；没有值就保持缺失，回读时如实标注。
            row.setdefault("操作人账号快照", row.get("操作人"))
            row.setdefault("原始提交时间", row.get("操作时间"))
            row.setdefault("复核人", None)
            row.setdefault("复核时间", None)
            row.setdefault("复核意见", None)
            # 既有的已归档种子数据没有指纹：启动时按历史事实只读补算一次，
            # 之后回读只做校验，不再生成任何可编辑副本。
            if row.get("status") == STATUS_ARCHIVED and not row.get("归档指纹"):
                row["归档指纹"] = fingerprint(row)
            key = str(row.get("事件链") or row.get("记录编号") or "")
            if key:
                self._event_chains.setdefault(key, []).append(int(row["id"]))

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def append(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """整体登记一行；写入完成才更新事件链索引。失败时抛 StoreWriteError。"""
        if module in _FAILING_MODULES:
            raise StoreWriteError(f"模块 {module} 持久化失败")
        rows = self.rows(module)
        rows.append(row)
        chain_key = str(row.get("事件链") or row.get("记录编号") or "")
        if module == "audit" and chain_key:
            self._event_chains.setdefault(chain_key, []).append(int(row["id"]))
        return row

    def chain(self, module: str, chain_key: str) -> list[dict[str, Any]]:
        """返回同一审计事件的全部修订，按登记顺序排列。"""
        if module != "audit":
            return []
        ids = self._event_chains.get(str(chain_key), [])
        rows = {int(row["id"]): row for row in self.rows(module)}
        return [rows[entry_id] for entry_id in ids if entry_id in rows]

    def register_chain(self, module: str, chain_key: str, entry_id: int) -> None:
        """兼容既有数据：把已经存在但未入链的审计事件补进索引。"""
        if module != "audit" or not chain_key:
            return
        chain = self._event_chains.setdefault(str(chain_key), [])
        if entry_id not in chain:
            chain.append(entry_id)

    def fail_module(self, module: str, enabled: bool = True) -> None:
        """本地演练开关：让指定模块的下一次写入失败。"""
        if enabled:
            _FAILING_MODULES.add(module)
        else:
            _FAILING_MODULES.discard(module)

    def reset_failures(self) -> None:
        _FAILING_MODULES.clear()

    def assert_writable(self, module: str) -> None:
        """写入前置检查：模拟持久化层不可用时立刻抛错，业务侧据此返回失败。"""
        if module in _FAILING_MODULES:
            raise StoreWriteError(f"模块 {module} 持久化失败")

    def assert_all_writable(self, *modules: str) -> None:
        for module in modules:
            self.assert_writable(module)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """最小事务：异常时同时回滚业务表与事件链索引。"""
        backup_tables = deepcopy(self._tables)
        backup_chains = deepcopy(self._event_chains)
        try:
            yield
        except Exception:
            self._tables = backup_tables
            self._event_chains = backup_chains
            raise

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
