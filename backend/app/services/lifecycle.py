"""表生命周期管理业务规则：状态流转、审计写入与治理清单。

生命周期动作不再只改一个状态：每个动作都在同一事务里写一条审计事件，
事件同时带上变更对象、类型、前后值、操作人与操作时间。审计写失败时，
业务状态随事务回滚，接口返回失败，不会出现“状态改了但审计没写”的断点。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.audit_integrity import STATUS_ARCHIVED, WRITE_LOCK
from app.services.audit import AuditError, audit_service
from app.store import StoreWriteError, store

MODULE = "lifecycle"
REQUIRED_FIELDS = ["策略编号", "表名称", "创建日期"]
STATUS_ORDER = ["草稿", "生效中", "已到期", STATUS_ARCHIVED]

# 动作 -> (可执行的起始状态, 目标状态, 变更类型)
ACTION_RULES: dict[str, tuple[list[str], str, str]] = {
    "启用策略": (["草稿"], "生效中", "启用策略"),
    "归档表": (["生效中", "已到期"], STATUS_ARCHIVED, "归档表"),
    "删除表": (["生效中"], "已到期", "删除表"),
}
NEGATIVE_ACTIONS = ["删除表"]


class LifecycleService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._view(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._view(row, with_sources=True) if row else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with WRITE_LOCK:
            entry = {"id": store.next_id(MODULE), "版本": 0}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["策略状态"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            store.append(MODULE, entry)
        return self._view(entry), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        operator: str = "值班管理员",
        remark: str | None = None,
        expected_version: int | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """执行生命周期动作；返回 (记录, 说明, 是否成功)。"""
        with WRITE_LOCK:
            row = store.find(MODULE, entry_id)
            if row is None:
                return None, f"生命周期策略 {entry_id} 不存在", False
            if action not in ACTION_RULES:
                return self._view(row, with_sources=True), f"动作「{action}」不属于表生命周期管理可执行范围", False

            allowed_from, target, change_type = ACTION_RULES[action]
            current = str(row.get("status") or "")
            if current == STATUS_ARCHIVED:
                return self._view(row, with_sources=True), "该生命周期策略已归档且只读，不能再次执行动作", False
            if current == target:
                # 重复动作：不重复写审计，回放最近一条同类型事件。
                replay = self._latest_event(entry_id, change_type)
                return (
                    self._view(row, with_sources=True),
                    f"策略已处于「{target}」，重复动作未新增记录"
                    + (f"，来源事件 {replay.get('记录编号')}" if replay else ""),
                    True,
                )
            if current not in allowed_from:
                return (
                    self._view(row, with_sources=True),
                    f"当前状态「{current}」不允许执行「{action}」，允许的起始状态：{'、'.join(allowed_from)}",
                    False,
                )

            operated_at = datetime.now().isoformat(timespec="seconds")
            # 对象版本 = 同一张表在生命周期与资产两个模块里的最高版本。
            object_version = max(
                int(other.get("版本") or 0)
                for other in (*store.rows(MODULE), *store.rows("catalog"))
                if str(other.get("表名称") or "") == str(row.get("表名称") or "")
            )
            # 页面提交时带了期望版本：说明操作人看到的是哪个版本，过期则拒绝写入，
            # 避免两个页面各自基于旧快照各改一次。
            if expected_version is not None and int(expected_version) != object_version:
                return (
                    self._view(row, with_sources=True),
                    f"页面数据版本 v{expected_version} 已过期，当前对象版本为 v{object_version}，请刷新后重试；策略状态未变更",
                    False,
                )
            event: dict[str, Any] | None = None
            after_version = object_version + 1
            try:
                with store.transaction():
                    store.assert_all_writable(MODULE, "audit")
                    event = audit_service.record_business_change(
                        source_module=MODULE,
                        source_key=str(row.get("策略编号") or entry_id),
                        object_key=str(row.get("表名称") or f"lifecycle:{entry_id}"),
                        object_pk=int(row["id"]),
                        change_type=change_type,
                        before=current,
                        after=target,
                        operator=operator,
                        operated_at=operated_at,
                        base_version=object_version,
                        result_version=after_version,
                        related_sources=[
                            {
                                "模块": MODULE,
                                "记录编号": str(row.get("策略编号") or entry_id),
                                "主键": int(row["id"]),
                            }
                        ],
                    )
                    row["status"] = target
                    # 策略状态是列表页直接展示的字段，必须跟状态流转同步，
                    # 否则会出现“状态已生效中、策略状态仍是草稿”的显示错位。
                    row["策略状态"] = target
                    row["pending"] = target != STATUS_ARCHIVED
                    row["abnormal"] = action in NEGATIVE_ACTIONS
                    row["最后操作"] = f"{action}（来源事件 {event.get('记录编号')}）"
                    row["版本"] = after_version
                    row["来源事件编号"] = event.get("记录编号")
            except (AuditError, StoreWriteError) as error:
                message = getattr(error, "message", str(error))
                return self._view(row, with_sources=True), f"{message}；策略状态未变更", False
            return (
                self._view(row, with_sources=True),
                f"生命周期策略已{action}，审计事件 {event.get('记录编号') if event else ''} 已写入",
                True,
            )

    def governance(self) -> dict[str, Any]:
        """生命周期治理清单：直接读取审计事件，不复制、不回填业务对象。"""
        summary = audit_service.governance(MODULE)
        summary["策略"] = [
            {
                "策略编号": row.get("策略编号"),
                "表名称": row.get("表名称"),
                "策略状态": row.get("status"),
                "来源事件": row.get("来源事件"),
                "版本": row.get("版本"),
            }
            for row in store.rows(MODULE)
        ]
        return summary

    def source_events(self, entry_id: int) -> list[dict[str, Any]]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return []
        return audit_service.source_panel(MODULE, str(row.get("策略编号") or entry_id))

    # ------------------------------------------------------------------ 内部
    def _latest_event(self, entry_id: int, change_type: str) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        events = audit_service.source_panel(MODULE, str(row.get("策略编号") or entry_id))
        for event in reversed(events):
            if event.get("变更类型") == change_type:
                return event
        return None

    def _view(self, row: dict[str, Any], *, with_sources: bool = False) -> dict[str, Any]:
        view = dict(row)
        view["只读"] = view.get("status") == STATUS_ARCHIVED
        # 页面拿到的版本必须是跨模块统一的对象版本，否则提交时会基于过期版本。
        view["版本"] = max(
            int(other.get("版本") or 0)
            for other in (*store.rows(MODULE), *store.rows("catalog"))
            if str(other.get("表名称") or "") == str(row.get("表名称") or "")
        )
        events = audit_service.source_panel(MODULE, str(row.get("策略编号") or row.get("id")))
        view["来源事件编号"] = events[-1].get("记录编号") if events else None
        if with_sources:
            view["来源事件"] = events
        return view


lifecycle_service = LifecycleService()
