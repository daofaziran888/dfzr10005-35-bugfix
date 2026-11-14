"""变更审计追踪业务规则：状态机、事件写入、归档只读与回读校验。

核心约束：
1. 审计事件严格按 已记录 -> 已复核 -> 已归档 流转，归档后只读；
2. 业务侧任何动作都必须把 对象、类型、前后值、操作人、操作时间 写进同一条事件；
3. 重复动作/并发争抢只接受一个版本，落库失败时业务动作不得显示成功；
4. 恢复与后续修订只能生成新的审计事件（修订引用），不得覆盖已归档事件；
5. 归档摘要用只读指纹校验，回读时逐项核对快照字段，缺失就标注，不临时补值。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from app.audit_integrity import (
    STATUS_ARCHIVED,
    STATUS_ORDER,
    STATUS_RECORDED,
    STATUS_REVIEWED,
    WRITE_LOCK,
    fingerprint,
    is_blank,
    missing_readback,
    readback_checks,
)
from app.store import StoreWriteError, store

MODULE = "audit"

# 业务动作必须同时落到同一条事件里的字段。
REQUIRED_EVENT_FIELDS = ["变更对象", "变更类型", "变更前值", "变更后值", "操作人", "操作时间"]

ACTION_RULES = {
    "标记复核": {"from": [STATUS_RECORDED], "to": STATUS_REVIEWED},
    "归档记录": {"from": [STATUS_REVIEWED], "to": STATUS_ARCHIVED},
    "恢复操作": {"from": [STATUS_ARCHIVED], "to": STATUS_RECORDED},
}


class AuditError(Exception):
    """审计写入失败：由路由层转成 ok=False 的可读说明，业务写入必须随事务回滚。"""

    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _value_matches(event_value: Any, current_value: Any) -> bool:
    """比对事件值与业务字段值：业务字段通常是事件描述里的一个片段。

    例如事件后值「保留期 90 天 / 数据等级 L3」应命中 LIFE-0002 的「90」
    和 CATA-0002 的「L3」；反过来业务字段是整句时也允许包含关系。
    """
    if event_value is None or current_value is None:
        return False
    event_text = str(event_value).strip()
    current_text = str(current_value).strip()
    if not event_text or not current_text:
        return False
    return current_text in event_text or event_text in current_text


class AuditService:
    # ------------------------------------------------------------------ 读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        module: str | None = None,
        source_key: str | None = None,
        include_archived: bool = True,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = [self._normalize(row) for row in store.rows(MODULE)]
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("记录编号", ""))
                or keyword in str(row.get("变更对象", ""))
                or keyword in str(row.get("来源记录编号", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if module:
            rows = [row for row in rows if self._belongs_to(row, module, source_key)]
        if not include_archived:
            rows = [row for row in rows if row.get("status") != STATUS_ARCHIVED]
        # 同一 id 只出现一次，避免重复动作把同一条事件显示成多行；
        # 归档后的修订是独立事件，用「修订序号」区分，不会被折叠掉。
        unique: dict[int, dict[str, Any]] = {int(row["id"]): row for row in rows}
        rows = sorted(unique.values(), key=lambda item: int(item["id"]), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._list_view(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._detail_view(self._normalize(row)) if row else None

    def history(self, entry_id: int) -> list[dict[str, Any]]:
        row = store.find(MODULE, entry_id)
        if row is None:
            return []
        normalized = self._normalize(row)
        chain = store.chain(MODULE, str(normalized.get("事件链")))
        if not chain:
            chain = [row]
        return [self._list_view(self._normalize(item)) for item in chain]

    def business_check(self, entry_id: int) -> dict[str, Any] | None:
        """把事件前后值与来源业务对象的当前字段值做对照。

        用于核对 AUDI-0002 的变更对象/类型/操作人/操作时间/复核状态，
        以及 LIFE-0002、CATA-0002 两个业务页面是否展示了同一对前后值。
        只读比对，不修正业务行，也不把事件值写回业务对象。
        """
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        entry = self._normalize(row)
        after_value = entry.get("变更后值")
        before_value = entry.get("变更前值")
        sources = []
        for link in entry.get("关联来源") or []:
            module = link.get("模块")
            key = link.get("记录编号")
            target_field = link.get("字段")
            # 主来源 = 事件真正发生的模块+记录；其余关联只是同一对象的上下文引用，
            # 不要求它体现本事件的前后值，否则会把 LIFE-0002 的保留期误读成资产等级变更。
            is_primary = module == entry.get("来源模块") and key == entry.get("来源记录编号")
            field_name = {"lifecycle": "策略编号", "catalog": "资产编号"}.get(str(module))
            source_row = store.find_by(str(module), field_name, key) if field_name else None
            current_value = source_row.get(target_field) if source_row and target_field else None
            current_values = (
                {
                    name: value
                    for name, value in source_row.items()
                    if name not in {"id", "pending", "abnormal", "status", "版本"}
                }
                if source_row
                else {}
            )
            hit_after = _value_matches(after_value, current_value) if current_value is not None else False
            hit_before = _value_matches(before_value, current_value) if current_value is not None else False
            if not is_primary:
                note = f"同一变更对象的上下文引用；本事件前后值以 {entry.get('来源记录编号')} 为准"
            elif current_value is None:
                note = f"来源记录未标注核对字段（{target_field or '未指定'}），无法对照前后值"
            elif hit_after:
                note = f"业务页面字段「{target_field}」= {current_value}，与事件后值一致"
            elif hit_before:
                note = f"业务页面仍显示变更前值 {current_value}，业务写入未生效或展示的是历史快照"
            else:
                note = f"业务页面字段「{target_field}」= {current_value}，与事件前后值都不一致"
            sources.append(
                {
                    "模块": module,
                    "记录编号": key,
                    "主来源": is_primary,
                    "核对字段": target_field,
                    "当前值": current_values,
                    "命中变更后值": bool(hit_after),
                    "命中变更前值": bool(hit_before),
                    "说明": note,
                }
            )
        return {
            "id": entry.get("id"),
            "记录编号": entry.get("记录编号"),
            "变更对象": entry.get("变更对象"),
            "变更类型": entry.get("变更类型"),
            "操作人": entry.get("操作人"),
            "操作人账号快照": entry.get("操作人账号快照"),
            "操作时间": entry.get("操作时间"),
            "复核状态": entry.get("status"),
            "复核人": entry.get("复核人"),
            "复核时间": entry.get("复核时间"),
            "变更前值": entry.get("变更前值"),
            "变更后值": entry.get("变更后值"),
            "来源核对": sources,
        }

    def readback(self, entry_id: int) -> dict[str, Any] | None:
        """归档回读：列表、单条详情、资产来源面板三处必须使用同一份事件快照。"""
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        normalized = self._normalize(row)
        list_view = self._list_view(normalized)
        detail_view = self._detail_view(normalized)
        # 资产来源面板由业务页面按 模块+记录编号 回读，这里走同一条审计投影链路，
        # 不能拿业务对象当前行拼装，否则会出现“面板值是现值、列表值是历史值”的错位。
        links = normalized.get("关联来源") or []
        panel_link = next((link for link in links if link.get("模块") == "catalog"), None)
        if panel_link is None and links:
            panel_link = links[0]
        panel_module = panel_link.get("模块") if panel_link else normalized.get("来源模块")
        panel_key = panel_link.get("记录编号") if panel_link else normalized.get("来源记录编号")
        source_views = self.source_panel(panel_module, panel_key)
        source_view = next(
            (item for item in source_views if int(item.get("id") or 0) == entry_id),
            None,
        )
        projection_keys = [
            "复核人",
            "复核时间",
            "变更后值",
            "操作人账号快照",
            "变更对象主键",
            "来源记录编号",
            "原始提交时间",
        ]
        projections = {
            "归档列表": {key: list_view.get(key) for key in projection_keys},
            "单条详情": {key: detail_view.get(key) for key in projection_keys},
            "资产来源面板": (
                {key: source_view.get(key) for key in projection_keys} if source_view else None
            ),
        }
        comparable = [value for value in projections.values() if value is not None]
        consistent = bool(comparable) and all(value == comparable[0] for value in comparable)
        return {
            "记录编号": normalized.get("记录编号"),
            "id": normalized.get("id"),
            "只读": normalized.get("status") == STATUS_ARCHIVED,
            "指纹": normalized.get("归档指纹"),
            "指纹校验": normalized.get("指纹校验"),
            "投影": projections,
            "一致": consistent,
            "缺失项": missing_readback(normalized),
        }

    def source_panel(
        self,
        module: str | None,
        source_key: str | None,
        entry_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """业务页面“来源事件”面板：按来源模块+记录编号或事件 id 精确回读。"""
        if entry_id is not None:
            row = store.find(MODULE, entry_id)
            if row is None:
                return []
            normalized = self._normalize(row)
            if module and not self._belongs_to(normalized, module, source_key):
                return []
            return [self._source_view(normalized)]
        if not module:
            return []
        rows = [self._normalize(row) for row in store.rows(MODULE)]
        matched = [row for row in rows if self._belongs_to(row, module, source_key)]
        unique = {int(row["id"]): row for row in matched}
        ordered = sorted(unique.values(), key=lambda item: int(item["id"]))
        return [self._source_view(row) for row in ordered]

    def governance(self, module: str | None = None) -> dict[str, Any]:
        """生命周期治理清单/整体审计核对视图：只看汇总，不回写业务对象。

        按来源模块严格过滤：跨模块关联事件只出现在真正的来源模块清单里，
        避免同一事件在生命周期和资产两个治理清单里重复计数。
        """
        rows = [self._normalize(row) for row in store.rows(MODULE)]
        if module:
            rows = [row for row in rows if row.get("来源模块") == module]
        items = [
            {
                "id": row.get("id"),
                "记录编号": row.get("记录编号"),
                "变更对象": row.get("变更对象"),
                "变更类型": row.get("变更类型"),
                "复核状态": row.get("status"),
                "来源模块": row.get("来源模块"),
                "来源记录编号": row.get("来源记录编号"),
                "变更前值": row.get("变更前值"),
                "变更后值": row.get("变更后值"),
                "操作人": row.get("操作人"),
                "操作时间": row.get("操作时间"),
                "归档指纹": row.get("归档指纹"),
                "指纹校验": row.get("指纹校验"),
                "只读": row.get("status") == STATUS_ARCHIVED,
                "缺失项": missing_readback(row),
            }
            for row in rows
        ]
        return {
            "module": module or "audit",
            "total": len(items),
            "archived": sum(1 for row in rows if row.get("status") == STATUS_ARCHIVED),
            "reviewed": sum(1 for row in rows if row.get("status") == STATUS_REVIEWED),
            "recorded": sum(1 for row in rows if row.get("status") == STATUS_RECORDED),
            "items": items,
        }

    # ------------------------------------------------------------------ 写入
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """人工登记变更事件；必填字段缺失时说明原因而不是静默落一条空记录。"""
        payload = dict(values)
        missing = [field for field in REQUIRED_EVENT_FIELDS if is_blank(payload.get(field))]
        if missing:
            return None, missing
        operator = str(payload.get("操作人") or "").strip()
        try:
            with WRITE_LOCK, store.transaction():
                store.assert_writable(MODULE)
                entry = self._new_event(
                    values=payload,
                    status=STATUS_RECORDED,
                    operator=operator,
                    source_module=str(payload.get("来源模块") or "manual"),
                    source_key=str(payload.get("来源记录编号") or ""),
                    object_key=str(payload.get("对象键") or payload.get("变更对象") or ""),
                    object_pk=payload.get("变更对象主键"),
                    base_version=0,
                    result_version=1,
                )
                store.append(MODULE, entry)
        except (AuditError, StoreWriteError) as error:
            message = getattr(error, "message", str(error))
            return None, [message]
        return self._detail_view(self._normalize(entry)), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        operator: str = "值班管理员",
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """账本动作：返回 (事件, 说明, 是否写入成功)。归档后只读。"""
        with WRITE_LOCK:
            row = store.find(MODULE, entry_id)
            if row is None:
                return None, f"变更记录 {entry_id} 不存在", False
            entry = self._normalize(row)
            if action not in ACTION_RULES:
                return self._detail_view(entry), f"动作「{action}」不属于变更审计追踪可执行范围", False

            current = entry.get("status")
            if current == STATUS_ARCHIVED and action != "恢复操作":
                return (
                    self._detail_view(entry),
                    f"该事件已归档且只读，不得再执行「{action}」，历史链保持不变",
                    False,
                )

            if action == "标记复核":
                if current == STATUS_REVIEWED:
                    opinion = str(remark or "").strip()
                    # 重复复核保留第一次结论，不新增记录、不改复核人/复核时间；
                    # 唯一允许的补充是历史记录缺失的复核意见，且必须由复核人本次真实填写。
                    if not entry.get("复核意见") and opinion:
                        try:
                            with store.transaction():
                                store.assert_writable(MODULE)
                                row["复核意见"] = opinion
                        except StoreWriteError as error:
                            return None, f"复核意见落库失败：{error}", False
                        return (
                            self._detail_view(self._normalize(row)),
                            "已补录缺失的复核意见，首次复核人与复核时间保持不变",
                            True,
                        )
                    return (
                        self._detail_view(entry),
                        "该事件已复核，保留首次复核结论，未新增记录",
                        True,
                    )
                if current != STATUS_RECORDED:
                    return self._detail_view(entry), f"当前状态「{current}」不允许标记复核", False
                opinion = str(remark or "").strip()
                if not opinion:
                    # 复核意见属于归档回读必查字段，必须在复核时由复核人真实填写。
                    return (
                        self._detail_view(entry),
                        "标记复核需要填写复核意见，不接受空意见或系统套话补全",
                        False,
                    )
                try:
                    with store.transaction():
                        store.assert_writable(MODULE)
                        row["复核人"] = operator
                        row["操作人账号快照"] = str(
                            entry.get("操作人账号快照") or entry.get("操作人") or operator
                        )
                        row["复核时间"] = datetime.now().isoformat(timespec="seconds")
                        row["复核意见"] = opinion
                        row["status"] = STATUS_REVIEWED
                        row["复核状态"] = STATUS_REVIEWED
                        row["pending"] = True
                        row["abnormal"] = False
                except StoreWriteError as error:
                    return None, f"复核落库失败：{error}，事件仍为已记录", False
                return self._detail_view(self._normalize(row)), "变更记录已标记复核", True

            if action == "归档记录":
                if current == STATUS_ARCHIVED:
                    # 重复归档：幂等回放，不改写归档指纹与历史链。
                    return (
                        self._detail_view(entry),
                        "该事件已归档且只读，重复归档请求不会改写历史链",
                        True,
                    )
                if current != STATUS_REVIEWED:
                    return (
                        self._detail_view(entry),
                        "事件必须先复核才能归档，已记录状态不能直接归档",
                        False,
                    )
                # 归档前必须补齐全部回读字段（含复核人、复核时间、复核意见），
                # 不能用“归档后再补”或当前账号/当前时间临时补全。
                missing = [item["标签"] for item in readback_checks(entry) if not item["存在"]]
                if missing:
                    return (
                        self._detail_view(entry),
                        f"归档回读字段缺失：{'、'.join(missing)}，请先补齐再归档，不得用当前账号或当前时间临时补全",
                        False,
                    )
                try:
                    with store.transaction():
                        store.assert_writable(MODULE)
                        row["归档时间"] = datetime.now().isoformat(timespec="seconds")
                        row["status"] = STATUS_ARCHIVED
                        row["复核状态"] = STATUS_ARCHIVED
                        row["pending"] = False
                        row["abnormal"] = False
                        row["归档指纹"] = fingerprint(self._normalize(row))
                        row["只读"] = True
                except StoreWriteError as error:
                    return None, f"归档落库失败：{error}，事件仍为已复核", False
                return self._detail_view(self._normalize(row)), "变更记录已归档，之后只读", True

            # 恢复操作：不修改原事件，只生成新的审计修订引用。
            if current != STATUS_ARCHIVED:
                return self._detail_view(entry), "只有已归档事件可以执行恢复操作", False
            try:
                with store.transaction():
                    store.assert_writable(MODULE)
                    revision = self._new_event(
                        values={
                            "变更对象": entry.get("变更对象"),
                            "变更类型": "恢复归档",
                            "变更前值": entry.get("状态快照") or entry.get("复核状态") or STATUS_ARCHIVED,
                            "变更后值": "已归档 -> 已记录（新建修订，原事件保持只读）",
                            "操作人": operator,
                            "操作时间": datetime.now().isoformat(timespec="seconds"),
                        },
                        status=STATUS_RECORDED,
                        operator=operator,
                        source_module=str(entry.get("来源模块") or "audit"),
                        source_key=str(entry.get("来源记录编号") or ""),
                        object_key=str(entry.get("对象键") or entry.get("变更对象") or ""),
                        object_pk=entry.get("变更对象主键"),
                        base_version=int(entry.get("结果版本") or 0),
                        result_version=int(entry.get("结果版本") or 0) + 1,
                        revision_of=int(entry["id"]),
                        chain_key=str(entry.get("事件链") or entry.get("记录编号") or ""),
                        related_sources=entry.get("关联来源") or [],
                    )
                    store.append(MODULE, revision)
            except StoreWriteError as error:
                return None, f"恢复落库失败：{error}，原归档事件未受影响", False
            return (
                self._detail_view(self._normalize(revision)),
                "恢复操作已生成新的审计修订引用，原归档事件保持不变",
                True,
            )

    def record_business_change(
        self,
        *,
        source_module: str,
        source_key: str,
        object_key: str,
        object_pk: Any,
        change_type: str,
        before: Any,
        after: Any,
        operator: str,
        operated_at: str,
        base_version: int,
        result_version: int,
        related_sources: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """业务动作写审计：字段不全抛错，写入失败抛错，由业务事务回滚并返回失败。"""
        values = {
            "变更对象": object_key,
            "变更类型": change_type,
            "变更前值": before,
            "变更后值": after,
            "操作人": operator,
            "操作时间": operated_at,
        }
        missing = [field for field in REQUIRED_EVENT_FIELDS if is_blank(values.get(field))]
        if missing:
            raise AuditError(f"审计事件缺少必填字段：{'、'.join(missing)}，业务动作未生效", 422)

        with WRITE_LOCK:
            # 同一对象写入权：对象版本取该对象全部审计事件里最高的结果版本。
            # 两个业务模块基于同一版本并发提交时，先提交者生效并抬升对象版本，
            # 后提交者的基底版本已过期，只接受先到的一个版本。
            current_version = self._object_version(object_key)
            if int(base_version) != current_version:
                raise AuditError(
                    f"变更对象「{object_key}」当前对象版本为 v{current_version}，本次提交基于 v{base_version}，"
                    "写入权已归属其他业务模块，请刷新后重试；请求未生效",
                    409,
                )

            # 重复动作：同一来源在同一结果版本上已经写入过，直接回放原事件，不新增。
            for row in store.rows(MODULE):
                normalized = self._normalize(row)
                if (
                    normalized.get("来源模块") == source_module
                    and normalized.get("来源记录编号") == source_key
                    and normalized.get("变更类型") == change_type
                    and int(normalized.get("结果版本") or 0) == int(result_version)
                ):
                    return self._detail_view(normalized)

            # 同一业务对象的历史链已归档时，不覆盖旧事件，生成修订引用。
            chain_key = self._chain_key(source_module, source_key)
            chain = store.chain(MODULE, chain_key)
            archived = [item for item in chain if item.get("status") == STATUS_ARCHIVED]
            revision_of = int(archived[-1]["id"]) if archived else None

            entry = self._new_event(
                values=values,
                status=STATUS_RECORDED,
                operator=operator,
                source_module=source_module,
                source_key=source_key,
                object_key=object_key,
                object_pk=object_pk,
                base_version=base_version,
                result_version=result_version,
                revision_of=revision_of,
                related_sources=related_sources or [],
                chain_key=chain_key,
            )
            store.append(MODULE, entry)
            self._object_owner[object_key] = {
                "owner": f"{source_module}:{source_key}",
                "version": int(result_version),
            }
            return self._detail_view(self._normalize(entry))

    # ------------------------------------------------------------------ 内部
    def __init__(self) -> None:
        # 变更对象键 -> 最后写入方与版本，用于“同一对象只接受一个版本”的写入权校验。
        self._object_owner: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _chain_key(source_module: str, source_key: str) -> str:
        return f"{source_module}:{source_key}"

    def _object_version(self, object_key: str) -> int:
        """变更对象当前版本 = 该对象所有审计事件里最高的结果版本。"""
        version = 0
        for row in store.rows(MODULE):
            if str(row.get("对象键") or row.get("变更对象") or "") == object_key:
                version = max(version, int(row.get("结果版本") or 0))
        return version

    def _belongs_to(self, row: dict[str, Any], module: str | None, source_key: str | None) -> bool:
        if not module:
            return False
        if row.get("来源模块") == module and (source_key is None or row.get("来源记录编号") == source_key):
            return True
        return any(
            link.get("模块") == module and (source_key is None or link.get("记录编号") == source_key)
            for link in row.get("关联来源") or []
        )

    def _new_event(
        self,
        *,
        values: dict[str, Any],
        status: str,
        operator: str,
        source_module: str,
        source_key: str,
        object_key: str,
        object_pk: Any,
        base_version: int,
        result_version: int,
        revision_of: int | None = None,
        related_sources: list[dict[str, Any]] | None = None,
        chain_key: str | None = None,
    ) -> dict[str, Any]:
        rows = store.rows(MODULE)
        entry_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        record_no = self._next_record_no(rows)
        operated_at = str(values.get("操作时间") or datetime.now().isoformat(timespec="seconds"))
        entry: dict[str, Any] = {
            "id": entry_id,
            "status": status,
            "pending": status != STATUS_ARCHIVED,
            "abnormal": False,
            "记录编号": record_no,
            "事件链": chain_key or self._chain_key(source_module, source_key) or record_no,
            "修订序号": 1,
            "revision_of": revision_of,
            "变更对象": object_key or values.get("变更对象"),
            "对象键": object_key,
            "变更类型": values.get("变更类型"),
            "变更前值": values.get("变更前值"),
            "变更后值": values.get("变更后值"),
            "操作人": operator,
            "操作人账号快照": operator,
            "操作时间": operated_at,
            "原始提交时间": operated_at,
            "复核状态": status,
            "复核人": None,
            "复核时间": None,
            "复核意见": None,
            "归档时间": None,
            "归档指纹": None,
            "来源模块": source_module,
            "来源记录编号": source_key,
            "变更对象主键": object_pk,
            "基底版本": int(base_version),
            "结果版本": int(result_version),
            "关联来源": related_sources or [],
            "只读": False,
        }
        if revision_of is not None:
            prior = store.find(MODULE, revision_of)
            if prior:
                entry["修订序号"] = int(prior.get("修订序号") or 1) + 1
                entry["关联来源"] = prior.get("关联来源") or entry["关联来源"]
                entry["变更对象主键"] = entry["变更对象主键"] or prior.get("变更对象主键")
        return entry

    @staticmethod
    def _next_record_no(rows: list[dict[str, Any]]) -> str:
        numbers = []
        for row in rows:
            record_no = str(row.get("记录编号") or "")
            if record_no.startswith("AUDI-") and record_no[5:].isdigit():
                numbers.append(int(record_no[5:]))
        return f"AUDI-{(max(numbers, default=0) + 1):04d}"

    def _normalize(self, row: dict[str, Any]) -> dict[str, Any]:
        """兼容既有审计记录：只补结构和状态别名，不改历史前后值。"""
        entry = deepcopy(row)
        entry.setdefault("记录编号", f"AUDI-{int(entry.get('id', 0)):04d}")
        entry.setdefault("事件链", str(entry.get("记录编号")))
        raw_status = entry.get("status") or entry.get("复核状态") or STATUS_RECORDED
        if raw_status not in STATUS_ORDER:
            raw_status = STATUS_RECORDED
        entry["status"] = raw_status
        entry["复核状态"] = raw_status
        entry["操作人账号快照"] = entry.get("操作人账号快照") or entry.get("操作人")
        entry["原始提交时间"] = entry.get("原始提交时间") or entry.get("操作时间")
        for field in (
            "来源模块",
            "来源记录编号",
            "变更对象主键",
            "复核人",
            "复核时间",
            "复核意见",
            "归档时间",
            "对象键",
        ):
            entry.setdefault(field, None)
        entry["修订序号"] = int(entry.get("修订序号") or 1)
        entry["revision_of"] = entry.get("revision_of")
        entry["只读"] = raw_status == STATUS_ARCHIVED
        entry["pending"] = bool(entry.get("pending", raw_status != STATUS_ARCHIVED))
        entry["abnormal"] = bool(entry.get("abnormal", False))
        links = entry.get("关联来源")
        if not links and entry.get("来源模块") and entry.get("来源记录编号"):
            links = [
                {
                    "模块": entry.get("来源模块"),
                    "记录编号": entry.get("来源记录编号"),
                    "主键": entry.get("变更对象主键"),
                }
            ]
        entry["关联来源"] = links or []
        entry["缺失项"] = missing_readback(entry)
        if raw_status == STATUS_ARCHIVED:
            stored = entry.get("归档指纹")
            computed = fingerprint(entry)
            if not stored:
                # 既有归档记录没有指纹：按历史事实只读计算，不生成可编辑副本。
                entry["归档指纹"] = computed
                entry["指纹校验"] = "归档指纹已按历史事实补算"
            elif stored == computed:
                entry["指纹校验"] = "指纹一致"
            else:
                entry["指纹校验"] = "指纹不一致，归档摘要可能被改动"
        else:
            entry["指纹校验"] = "未归档，无归档指纹"
        return entry

    def _list_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        """归档列表投影：前后值、复核人、复核时间都取事件自身快照，不取业务对象当前值。"""
        keys = [
            "id",
            "记录编号",
            "修订序号",
            "revision_of",
            "变更对象",
            "变更类型",
            "变更前值",
            "变更后值",
            "操作人",
            "操作人账号快照",
            "操作时间",
            "原始提交时间",
            "复核人",
            "复核时间",
            "复核意见",
            "复核状态",
            "status",
            "pending",
            "abnormal",
            "只读",
            "来源模块",
            "来源记录编号",
            "变更对象主键",
            "归档时间",
            "归档指纹",
            "指纹校验",
            "缺失项",
        ]
        return {key: entry.get(key) for key in keys}

    def _detail_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        view = self._list_view(entry)
        view["关联来源"] = entry.get("关联来源") or []
        view["基底版本"] = entry.get("基底版本")
        view["结果版本"] = entry.get("结果版本")
        view["回读校验"] = readback_checks(entry)
        view["归档只读"] = entry.get("status") == STATUS_ARCHIVED
        view["可执行动作"] = self._available_actions(entry)
        return view

    def _source_view(self, entry: dict[str, Any]) -> dict[str, Any]:
        """来源面板投影：与列表/详情共用同一份字段值，避免三处显示不一致。"""
        view = self._list_view(entry)
        view["归档只读"] = entry.get("status") == STATUS_ARCHIVED
        view["可执行动作"] = self._available_actions(entry)
        return view

    @staticmethod
    def _available_actions(entry: dict[str, Any]) -> list[str]:
        status = entry.get("status")
        if status == STATUS_ARCHIVED:
            return ["恢复操作"]
        if status == STATUS_REVIEWED:
            return ["归档记录"]
        return ["标记复核"]


audit_service = AuditService()
