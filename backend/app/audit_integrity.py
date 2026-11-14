"""审计事件完整性工具：状态常量、归档指纹、回读校验与共享写入锁。

这个模块只依赖标准库，store、services.audit 与各业务服务都可以安全引用，不会形成循环导入。
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

STATUS_RECORDED = "已记录"
STATUS_REVIEWED = "已复核"
STATUS_ARCHIVED = "已归档"
STATUS_ORDER = [STATUS_RECORDED, STATUS_REVIEWED, STATUS_ARCHIVED]

# 业务模块动作与审计状态的跨模块关联：生命周期、资产等模块的动作统一走这张表，
# 让“业务动作 -> 审计事件”有且只有一条可追踪的语义。
WRITE_LOCK = threading.RLock()

# 参与归档指纹计算的字段。只覆盖不可变事实，不覆盖状态、只读标记、校验结论等回读派生字段，
# 因此归档后反复回读得到同一指纹，也不会因为展示层加工而变化。
FINGERPRINT_FIELDS = [
    "记录编号",
    "变更对象",
    "变更类型",
    "变更前值",
    "变更后值",
    "操作人",
    "操作时间",
    "复核人",
    "复核时间",
    "复核意见",
    "来源模块",
    "来源记录编号",
    "变更对象主键",
    "原始提交时间",
]

# 归档回读必须逐项核对的字段：缺任何一项都要在审核面板标明，不能用当前账号或当前时间补全。
# 键是审计事件里的真实字段名，值是展示标签。
READBACK_FIELDS: list[tuple[str, str]] = [
    ("变更对象", "变更对象"),
    ("变更前值", "变更前值"),
    ("操作人账号快照", "操作人账号快照"),
    ("复核人", "复核人"),
    ("复核时间", "复核时间"),
    ("复核意见", "复核意见"),
    ("变更对象主键", "变更对象主键"),
    ("来源记录编号", "来源记录编号"),
    ("原始提交时间", "原始提交时间"),
    ("变更后值", "变更后值"),
]


def is_blank(value: Any) -> bool:
    """判断字段是否可视为缺失；0 与 False 是有效值，只有 None/空串算缺失。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def canonical_payload(entry: dict[str, Any]) -> str:
    """把不可变事实序列化成稳定字符串，保证同一事件每次校验得到同一指纹。"""
    payload = {field: entry.get(field) for field in FINGERPRINT_FIELDS}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(entry: dict[str, Any]) -> str:
    """归档摘要的只读指纹：只做校验，不产生任何可编辑副本。"""
    return hashlib.sha256(canonical_payload(entry).encode("utf-8")).hexdigest()


def readback_checks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """逐项核对归档回读必需字段，缺失时给出可读说明而不是临时补值。"""
    archived = entry.get("status") == STATUS_ARCHIVED
    checks: list[dict[str, Any]] = []
    for field, label in READBACK_FIELDS:
        value = entry.get(field)
        present = not is_blank(value)
        if present:
            note = "已读取归档快照"
        elif archived:
            note = f"{label}缺失，需人工补录，不得用当前账号或当前时间临时补全"
        else:
            note = f"{label}尚未产生，归档前必须补齐"
        checks.append(
            {
                "字段": field,
                "标签": label,
                "存在": present,
                "值": value if present else None,
                "说明": note,
            }
        )
    return checks


def missing_readback(entry: dict[str, Any]) -> list[str]:
    """返回确实缺失、且无法用事件自身事实解释的回读字段标签。

    已归档事件逐项全查，缺任何一项都要标注；未归档事件不把尚未产生的复核字段
    算作缺失，也不把“草稿 -> 生效中”里合法的空变更前值误报为数据缺失。
    """
    if entry.get("status") == STATUS_ARCHIVED:
        return [item["标签"] for item in readback_checks(entry) if not item["存在"]]
    pre_review_fields = {"复核人", "复核时间", "复核意见"}
    return [
        item["标签"]
        for item in readback_checks(entry)
        if not item["存在"] and item["标签"] not in pre_review_fields
    ]
