"""变更审计追踪接口：维护变更记录，覆盖标记复核、归档记录、恢复操作与回读校验。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.audit import audit_service

router = APIRouter(prefix="/api/audit", tags=["变更审计追踪"])

service = audit_service

LIST_FIELDS = ["记录编号", "变更对象", "变更类型", "变更前值", "变更后值", "操作人", "操作时间", "复核状态"]
STATUSES = ["已记录", "已复核", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按记录编号、变更对象或来源记录编号检索"),
    status: str | None = Query(default=None, description="已记录、已复核、已归档"),
    module: str | None = Query(default=None, description="按来源模块过滤，如 lifecycle、catalog"),
    source_key: str | None = Query(default=None, description="按来源记录编号过滤"),
    include_archived: bool = Query(default=True, description="是否包含已归档事件"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按记录编号与状态过滤变更审计追踪列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(
        keyword=keyword,
        status=status,
        module=module,
        source_key=source_key,
        include_archived=include_archived,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/governance", response_model=dict)
def governance(module: str | None = Query(default=None, description="按来源模块过滤治理清单")) -> dict:
    """生命周期治理清单：汇总事件状态、归档指纹与回读缺失项。"""
    return service.governance(module)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出变更审计追踪清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "audit", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条变更记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"变更记录 {entry_id} 不存在")
    return entry


@router.get("/{entry_id}/history", response_model=dict)
def get_history(entry_id: int) -> dict:
    """读取同一事件链的全部修订；归档事件仍在链中保持只读。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"变更记录 {entry_id} 不存在")
    return {"id": entry_id, "items": service.history(entry_id)}


@router.get("/{entry_id}/readback", response_model=dict)
def get_readback(entry_id: int) -> dict:
    """归档回读：核对归档列表、单条详情、资产来源面板三处投影是否完全一致。"""
    result = service.readback(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"变更记录 {entry_id} 不存在")
    return result


@router.get("/{entry_id}/business-check", response_model=dict)
def get_business_check(entry_id: int) -> dict:
    """核对事件前后值与 LIFE-0002、CATA-0002 等来源业务页面的当前字段值。"""
    result = service.business_check(entry_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"变更记录 {entry_id} 不存在")
    return result


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条变更记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"变更记录登记失败：{'、'.join(missing)}")
    return ActionResult(ok=True, message="变更记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行标记复核、归档记录、恢复操作；归档后只读，重复动作保留首次结论。"""
    action = payload.action_name()
    entry, message, ok = service.run_action(
        entry_id,
        action,
        operator=payload.operator_name(),
        remark=payload.remark,
    )
    return ActionResult(ok=ok, message=message, entry=entry)
