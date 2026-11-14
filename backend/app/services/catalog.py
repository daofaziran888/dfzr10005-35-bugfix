"""数据资产盘点业务规则：状态流转、审计写入与来源事件回读。

资产动作与生命周期动作共用同一套审计写入约束：
同一事务内先写审计事件再更新业务行；审计写失败则业务行回滚；
同一资产对象的并发写入按基底版本只接受一个版本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.audit_integrity import STATUS_ARCHIVED, WRITE_LOCK
from app.services.audit import AuditError, audit_service
from app.store import StoreWriteError, store

MODULE = "catalog"
REQUIRED_FIELDS = ["资产编号", "表名称", "资产分类"]
STATUS_ORDER = ["草稿", "已盘点", "已复核", STATUS_ARCHIVED]

# 动作 -> (可执行的起始状态, 目标状态, 变更类型)
ACTION_RULES: dict[str, tuple[list[str], str, str]] = {
    "提交复核": (["草稿", "已盘点"], "已盘点", "提交复核"),
    "归档资产": (["已盘点", "已复核"], "已复核", "归档资产"),
    "废弃资产": (["已复核"], STATUS_ARCHIVED, "废弃资产"),
}
NEGATIVE_ACTIONS = ["废弃资产"]


class CatalogService:
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
            rows = [row for row in rows if keyword in str(row.get("资产编号", ""))]
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
        with WRITE_LOCK:
            row = store.find(MODULE, entry_id)
            if row is None:
                return None, f"资产记录 {entry_id} 不存在", False
            if action not in ACTION_RULES:
                return self._view(row, with_sources=True), f"动作「{action}」不属于数据资产盘点可执行范围", False

            allowed_from, target, change_type = ACTION_RULES[action]
            current = str(row.get("status") or "")
            if current == STATUS_ARCHIVED:
                return self._view(row, with_sources=True), "该资产记录已归档且只读，不能再次执行动作", False
            if current == target:
                replay = self._latest_event(entry_id, change_type)
                return (
                    self._view(row, with_sources=True),
                    f"资产已处于「{target}」，重复动作未新增记录"
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
                for other in (*store.rows(MODULE), *store.rows("lifecycle"))
                if str(other.get("表名称") or "") == str(row.get("表名称") or "")
            )
            if expected_version is not None and int(expected_version) != object_version:
                return (
                    self._view(row, with_sources=True),
                    f"页面数据版本 v{expected_version} 已过期，当前对象版本为 v{object_version}，请刷新后重试；资产状态未变更",
                    False,
                )
            after_version = object_version + 1
            event: dict[str, Any] | None = None
            try:
                with store.transaction():
                    store.assert_all_writable(MODULE, "audit")
                    event = audit_service.record_business_change(
                        source_module=MODULE,
                        source_key=str(row.get("资产编号") or entry_id),
                        object_key=str(row.get("表名称") or f"catalog:{entry_id}"),
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
                                "记录编号": str(row.get("资产编号") or entry_id),
                                "主键": int(row["id"]),
                            }
                        ],
                    )
                    row["status"] = target
                    row["pending"] = target != STATUS_ARCHIVED
                    row["abnormal"] = action in NEGATIVE_ACTIONS
                    row["版本"] = after_version
            except (AuditError, StoreWriteError) as error:
                message = getattr(error, "message", str(error))
                return self._view(row, with_sources=True), f"{message}；资产状态未变更", False
            return (
                self._view(row, with_sources=True),
                f"资产记录已{action}，审计事件 {event.get('记录编号') if event else ''} 已写入",
                True,
            )

    def source_events(self, entry_id: int) -> list[dict[str, Any]]:
        """资产页面来源面板：返回审计事件快照，前后值与复核信息保持归档时的值。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            return []
        return audit_service.source_panel(MODULE, str(row.get("资产编号") or entry_id))

    def governance(self) -> dict[str, Any]:
        summary = audit_service.governance(MODULE)
        summary["资产"] = [
            {
                "资产编号": row.get("资产编号"),
                "表名称": row.get("表名称"),
                "资产状态": row.get("status"),
                "版本": row.get("版本"),
            }
            for row in store.rows(MODULE)
        ]
        return summary

    # ------------------------------------------------------------------ 内部
    def _latest_event(self, entry_id: int, change_type: str) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        events = audit_service.source_panel(MODULE, str(row.get("资产编号") or entry_id))
        for event in reversed(events):
            if event.get("变更类型") == change_type:
                return event
        return None

    def _view(self, row: dict[str, Any], *, with_sources: bool = False) -> dict[str, Any]:
        view = dict(row)
        view["只读"] = view.get("status") == STATUS_ARCHIVED
        # 与生命周期共用同一个对象版本，保证页面提交的 expected_version 能对得上。
        view["版本"] = max(
            int(other.get("版本") or 0)
            for other in (*store.rows(MODULE), *store.rows("lifecycle"))
            if str(other.get("表名称") or "") == str(row.get("表名称") or "")
        )
        events = audit_service.source_panel(MODULE, str(row.get("资产编号") or row.get("id")))
        view["来源事件编号"] = events[-1].get("记录编号") if events else None
        if with_sources:
            view["来源事件"] = events
        return view


catalog_service = CatalogService()
