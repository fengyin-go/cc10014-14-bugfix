"""响应转换接口：维护转换模板，覆盖登记、更新、提交校验、启用模板、停用模板与转换执行。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.transform import TransformService

router = APIRouter(prefix="/api/transform", tags=["响应转换"])

service = TransformService()

LIST_FIELDS = ["模板编号", "关联服务", "输入字段", "输出字段", "格式转换", "默认填充", "数组展开", "嵌套打平"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤响应转换列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出响应转换清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "transform", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条转换模板明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"转换模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条转换模板：全部字段一起落库；同一模板编号重复提交只保留一条有效记录。"""
    entry, missing, created = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    if not created:
        return ActionResult(ok=True, message="相同模板编号已登记过，复用原有记录", entry=entry)
    return ActionResult(ok=True, message="转换模板已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """整体更新模板配置：未提交的字段落默认值，避免旧值残留；已启用模板需先停用。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条转换模板执行提交校验、启用模板、停用模板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/apply", response_model=ActionResult)
def apply_template(entry_id: int, payload: EntryPayload) -> ActionResult:
    """按完整模板配置转换查询结果：打平嵌套、展开数组、填充默认值，并落库转换记录。"""
    records = payload.values.get("records", payload.values.get("数据"))
    record_entry, message = service.apply_template(entry_id, records)
    if record_entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record_entry)


@router.get("/{entry_id}/records")
def list_records(entry_id: int) -> dict[str, Any]:
    """列出该模板的全部转换记录，用于从转换后的查询数据集逐列复核。"""
    records, message = service.list_records(entry_id)
    if records is None:
        raise HTTPException(status_code=404, detail=message)
    return {"items": records, "total": len(records)}


@router.get("/{entry_id}/contract")
def get_contract(entry_id: int) -> dict[str, Any]:
    """读取转换模板契约：模板字段与同步到接口文档的字段映射。"""
    contract, message = service.contract(entry_id)
    if contract is None:
        raise HTTPException(status_code=404, detail=message)
    return contract
