"""响应转换业务规则：状态流转、字段校验、嵌套打平与转换记录都收在这里。

修复要点（对应可复现错误）：
- 登记/更新把全部模板字段一起落库，输出字段、格式转换、嵌套打平不再丢失；
- 状态机强制 草稿→待校验→已启用→已停用，模板不能跳过校验直接启用；
- 同一模板编号重复提交只保留一条有效记录（幂等登记）；
- 转换记录按模板串行落库，输出版本号单调递增，并发写只产生唯一版本；
- 同一输出字段多条规则冲突时按确定优先级仲裁（显式命名优先、其次源路径字典序），
  与录入顺序无关；
- 转换后的查询结果同步回接口文档（schema 模块），跨模块共用同一份字段映射。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "transform"
RECORD_MODULE = "transform_record"
SCHEMA_MODULE = "schema"

# 模板的全部业务字段：登记、更新、读取都按这份清单走，保证不丢列。
ALL_FIELDS = ["模板编号", "关联服务", "输入字段", "输出字段", "格式转换", "默认填充", "数组展开", "嵌套打平"]
REQUIRED_FIELDS = ["模板编号", "关联服务", "输入字段"]
# 提交校验前必须齐全的配置：输入字段、输出字段和格式转换必须一起落库，
# 否则转换记录无法解释每个输出值。
CONTRACT_FIELDS = ["输入字段", "输出字段", "格式转换"]
FIELD_DEFAULTS = {"默认填充": "", "数组展开": "否", "嵌套打平": "否"}

STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
# 动作 -> (允许的前置状态, 目标状态)：仲裁优先级看状态机，不看录入顺序。
ACTION_RULES = {
    "提交校验": (["草稿"], "待校验"),
    "启用模板": (["待校验"], "已启用"),
    "停用模板": (["已启用"], "已停用"),
}
NEGATIVE_ACTIONS = ["停用模板"]

# 全局写锁：登记去重、状态流转、转换记录落库都串行化，
# 并发写入同一转换记录时只允许一个输出版本落库。
_LOCK = threading.RLock()

_TRUTHY = {"是", "true", "1", "开启", "开", "yes", "y", "on"}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _split_fields(raw: Any) -> list[str]:
    """把逗号分隔的字段串解析成有序、去空白的列表；支持中英文逗号与顿号。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw).replace("，", ",").replace("、", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _truthy(raw: Any) -> bool:
    return str(raw or "").strip().lower() in _TRUTHY


def _parse_pair_map(raw: Any) -> dict[str, str]:
    """解析「字段:规则」形式的配置串，兼容中文冒号、等号与分号分隔。

    只有真正出现分隔符的片段才进入映射；裸值（如默认填充的 unknown）
    不当作键，由调用方按全局值处理。
    """
    result: dict[str, str] = {}
    if raw is None:
        return result
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip()}
    text = str(raw).replace("；", ";").replace("，", ",")
    for chunk in text.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        for sep in ("：", ":", "="):
            if sep in chunk:
                key, _, value = chunk.partition(sep)
                key, value = key.strip(), value.strip()
                if key:
                    result[key] = value
                break
    return result


def _resolve_path(record: dict[str, Any], path: str) -> Any:
    """按点路径从嵌套结构里取值；任一层缺失都返回 None。"""
    current: Any = record
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        else:
            return None
    return current


def _convert_value(value: Any, rule: str) -> Any:
    """按格式转换规则处理单个值；规则为空或「无」时原样返回。"""
    rule = (rule or "").strip().lower()
    if rule in ("", "无", "none", "保持原样"):
        return value
    if value is None:
        return None
    if rule in ("upper", "大写"):
        return str(value).upper()
    if rule in ("lower", "小写"):
        return str(value).lower()
    if rule in ("trim", "去空格"):
        return str(value).strip()
    if rule in ("int", "整数"):
        return int(float(str(value)))
    if rule in ("float", "小数"):
        return float(str(value))
    if rule in ("str", "字符串"):
        return str(value)
    if rule in ("bool", "布尔"):
        return _truthy(value)
    if rule in ("date", "日期"):
        return str(value)[:10]
    if rule == "json":
        return json.dumps(value, ensure_ascii=False)
    # 未识别的转换规则不静默吞掉：保留原值，由调用方在转换记录里标注。
    raise ValueError(f"未识别的格式转换规则「{rule}」")


class TransformService:
    # ---------------------------------------------------------------- 查询
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
            rows = [row for row in rows if keyword in str(row.get("模板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def find_by_code(self, code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("模板编号", "")) == code:
                return row
        return None

    # ---------------------------------------------------------------- 登记
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], bool]:
        """登记模板。返回 (记录, 缺失字段, 是否新建)。

        同一模板编号重复提交时复用已有记录，只保留一条有效记录。
        """
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing, False
        with _LOCK:
            existing = self.find_by_code(str(values["模板编号"]).strip())
            if existing is not None:
                return existing, [], False
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            # 全部业务字段一起落库，缺省值补齐，不再静默丢弃输出字段等配置。
            for field in ALL_FIELDS:
                entry[field] = values.get(field, FIELD_DEFAULTS.get(field, ""))
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            return entry, [], True

    # ---------------------------------------------------------------- 更新
    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """整体替换模板业务字段：未提交的字段落默认值，杜绝旧值残留。"""
        with _LOCK:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"转换模板 {entry_id} 不存在或已归档"
            if entry.get("status") == "已启用":
                return None, "模板已启用，不能直接修改；请先停用模板再调整配置"
            new_code = str(values.get("模板编号", entry.get("模板编号", ""))).strip()
            if not new_code:
                return None, "模板编号不能为空"
            duplicated = self.find_by_code(new_code)
            if duplicated is not None and int(duplicated.get("id", 0)) != entry_id:
                return None, f"模板编号 {new_code} 已被另一条模板占用"
            for field in ALL_FIELDS:
                entry[field] = values.get(field, FIELD_DEFAULTS.get(field, ""))
            # 配置变更后必须重新校验，避免带着旧结论进入启用环节。
            if entry.get("status") == "待校验":
                entry["status"] = "草稿"
                entry["pending"] = True
            entry["abnormal"] = False
            return entry, "转换模板配置已更新，需重新提交校验"

    # ---------------------------------------------------------------- 状态流转
    def validate_contract(self, entry: dict[str, Any]) -> list[str]:
        """校验模板契约完整性：输入/输出/格式转换齐全，且输入输出逐列对齐。"""
        problems: list[str] = []
        for field in CONTRACT_FIELDS:
            if not str(entry.get(field) or "").strip():
                problems.append(f"缺少{field}")
        inputs = _split_fields(entry.get("输入字段"))
        outputs = _split_fields(entry.get("输出字段"))
        if inputs and outputs and len(inputs) != len(outputs):
            problems.append(f"输入字段 {len(inputs)} 个与输出字段 {len(outputs)} 个数量不一致，会造成输出列错位")
        return problems

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with _LOCK:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"转换模板 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于响应转换可执行范围"
            allowed_from, target = ACTION_RULES[action]
            current = str(entry.get("status", ""))
            if current not in allowed_from:
                expect = "、".join(allowed_from)
                return None, f"当前状态「{current}」不能执行{action}，请先流转到{expect}"
            if action in ("提交校验", "启用模板"):
                problems = self.validate_contract(entry)
                if problems:
                    entry["abnormal"] = True
                    return None, "模板契约不完整：" + "；".join(problems)
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return entry, f"转换模板已{action}"

    # ---------------------------------------------------------------- 转换处理
    def _build_mapping(self, entry: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        """把模板配置解析成逐列映射，并对同一输出列的多条规则做确定性仲裁。

        仲裁优先级（与录入顺序无关）：
        1. 在输出字段里被显式命名的映射优先；
        2. 其余按来源路径字典序，保证任何提交顺序都得到同一结果。
        """
        inputs = _split_fields(entry.get("输入字段"))
        outputs = _split_fields(entry.get("输出字段"))
        conversions = _parse_pair_map(entry.get("格式转换"))
        fills = _parse_pair_map(entry.get("默认填充"))
        global_fill = "" if fills else str(entry.get("默认填充") or "")
        flatten_on = _truthy(entry.get("嵌套打平"))

        candidates: list[dict[str, Any]] = []
        for index, source in enumerate(inputs):
            explicit = outputs[index] if index < len(outputs) else ""
            column = explicit or source.split(".")[-1]
            candidates.append({
                "输出列": column,
                "来源": source,
                "显式命名": bool(explicit),
                "格式转换": conversions.get(source) or conversions.get(column) or "",
                "默认填充": fills.get(source) or fills.get(column) or global_fill,
                "嵌套打平": flatten_on and "." in source,
            })

        # 冲突仲裁：同一输出列的多条规则按确定优先级选出唯一生效规则
        # （显式命名优先，其次来源路径字典序），与字段录入顺序无关；
        # 输出列顺序仍按模板声明顺序，保证转换结果与模板逐列一致。
        winners: dict[str, dict[str, Any]] = {}
        losers: dict[str, list[dict[str, Any]]] = {}
        for item in candidates:
            column = item["输出列"]
            current = winners.get(column)
            if current is None:
                winners[column] = item
                continue
            key = lambda c: (not c["显式命名"], c["来源"])
            keep, drop = (current, item) if key(current) <= key(item) else (item, current)
            winners[column] = keep
            losers.setdefault(column, []).append(drop)

        mapping: list[dict[str, Any]] = []
        conflicts: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            column = item["输出列"]
            if column in seen:
                continue
            seen.add(column)
            mapping.append(winners[column])
            for drop in losers.get(column, []):
                conflicts.append(
                    f"输出列「{column}」存在多条转换规则，已按确定性优先级采用「{winners[column]['来源']}」，丢弃「{drop['来源']}」"
                )
        return mapping, conflicts

    def _transform_record(
        self,
        record: dict[str, Any],
        mapping: list[dict[str, Any]],
        expand_arrays: bool,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """按映射转换单条原始记录；数组展开时一条输入可能产出多条输出。"""
        notes: list[str] = []
        row: dict[str, Any] = {}
        for item in mapping:
            value = _resolve_path(record, item["来源"])
            if value is None or value == "":
                value = item["默认填充"] if item["默认填充"] != "" else value
            elif item["格式转换"]:
                try:
                    value = _convert_value(value, item["格式转换"])
                except (ValueError, TypeError) as exc:
                    notes.append(f"列「{item['输出列']}」格式转换失败：{exc}，已保留原值")
            row[item["输出列"]] = value

        if not expand_arrays:
            # 不展开时数组也不能原样残留，序列化成逗号串，保证输出列结构唯一。
            for key, value in row.items():
                if isinstance(value, list):
                    row[key] = ",".join(str(v) for v in value)
            return [row], notes

        array_columns = [key for key, value in row.items() if isinstance(value, list)]
        if not array_columns:
            return [row], notes
        rows = [row]
        for column in array_columns:
            values = rows[0][column] or [None]
            expanded: list[dict[str, Any]] = []
            for existing in rows:
                for element in values:
                    new_row = dict(existing)
                    new_row[column] = element
                    expanded.append(new_row)
            rows = expanded
        return rows, notes

    def apply_template(
        self,
        entry_id: int,
        records: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, str]:
        """把查询结果的原始记录按完整模板配置转换，产出扁平的查询数据集。

        转换记录（含输入字段、输出字段、格式转换）随数据集一起落库，
        输出版本按模板单调递增；同一模板同一批数据重复提交只保留一条记录。
        """
        with _LOCK:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"转换模板 {entry_id} 不存在或已归档"
            if entry.get("status") != "已启用":
                return None, f"模板当前状态「{entry.get('status')}」不能执行转换，请先启用模板"
            if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
                return None, "待转换的查询结果必须是对象数组"

            fingerprint = json.dumps(records, ensure_ascii=False, sort_keys=True)
            table = store.rows(RECORD_MODULE)
            code = str(entry.get("模板编号", ""))
            for existing in table:
                if existing.get("模板编号") == code and existing.get("输入指纹") == fingerprint:
                    return existing, "同一批查询结果已转换过，复用已有转换记录"

            mapping, conflicts = self._build_mapping(entry)
            expand_arrays = _truthy(entry.get("数组展开"))
            columns = [item["输出列"] for item in mapping]

            dataset: list[dict[str, Any]] = []
            notes: list[str] = []
            for record in records:
                rows, row_notes = self._transform_record(record, mapping, expand_arrays)
                dataset.extend(rows)
                notes.extend(row_notes)

            version = max(
                (int(item.get("输出版本", 0)) for item in table if item.get("模板编号") == code),
                default=0,
            ) + 1
            record_entry = {
                "id": max((int(item.get("id", 0)) for item in table), default=0) + 1,
                "记录编号": f"TREC-{max((int(item.get('id', 0)) for item in table), default=0) + 1:04d}",
                "模板编号": code,
                "关联服务": entry.get("关联服务", ""),
                "输出版本": version,
                # 输入字段、输出字段和格式转换一起落库，转换记录才能解释每个输出值。
                "输入字段": entry.get("输入字段", ""),
                "输出字段": entry.get("输出字段", ""),
                "格式转换": entry.get("格式转换", ""),
                "默认填充": entry.get("默认填充", ""),
                "数组展开": entry.get("数组展开", ""),
                "嵌套打平": entry.get("嵌套打平", ""),
                "列映射": mapping,
                "输出列": columns,
                "行数": len(dataset),
                "数据集": dataset,
                "冲突说明": conflicts,
                "转换备注": notes,
                "输入指纹": fingerprint,
                "生成时间": _now(),
            }
            table.append(record_entry)

            # 跨模块台账同步：转换后的字段映射回写到接口文档，共用同一份契约。
            synced = self._sync_contract(entry, columns, mapping, version)
            record_entry["契约同步"] = synced
            return record_entry, f"转换完成：{len(dataset)} 行，输出版本 v{version}"

    def _sync_contract(
        self,
        entry: dict[str, Any],
        columns: list[str],
        mapping: list[dict[str, Any]],
        version: int,
    ) -> str:
        """把转换后的字段映射同步回接口文档模块，让文档与模板用同一字段映射。"""
        service = str(entry.get("关联服务", ""))
        contract = {
            "模板编号": entry.get("模板编号", ""),
            "输出版本": version,
            "输出列": columns,
            "字段映射": [
                {"输出列": item["输出列"], "来源": item["来源"], "格式转换": item["格式转换"]}
                for item in mapping
            ],
        }
        text = json.dumps(contract, ensure_ascii=False)
        targets = [row for row in store.rows(SCHEMA_MODULE) if str(row.get("关联服务", "")) == service]
        if not targets:
            rows = store.rows(SCHEMA_MODULE)
            targets = [{
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "定义编号": f"SCHE-{max((int(row.get('id', 0)) for row in rows), default=0) + 1:04d}",
                "关联服务": service,
                "请求方法": "GET",
                "status": "草稿",
                "pending": True,
                "abnormal": False,
            }]
            rows.append(targets[0])
        for target in targets:
            target["参数列表"] = ",".join(columns)
            target["响应示例"] = text
            target["字段映射"] = contract
        entry["契约版本"] = version
        entry["最近输出列"] = ",".join(columns)
        return "已同步接口文档：" + "、".join(str(item.get("定义编号", "")) for item in targets)

    # ---------------------------------------------------------------- 复核
    def list_records(self, entry_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"转换模板 {entry_id} 不存在或已归档"
        code = str(entry.get("模板编号", ""))
        records = [item for item in store.rows(RECORD_MODULE) if item.get("模板编号") == code]
        records.sort(key=lambda item: int(item.get("输出版本", 0)))
        return records, "ok"

    def contract(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """读取转换模板契约：模板字段、最近转换版本与接口文档同步情况。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"转换模板 {entry_id} 不存在或已归档"
        service = str(entry.get("关联服务", ""))
        docs = [
            {
                "定义编号": row.get("定义编号", ""),
                "参数列表": row.get("参数列表", ""),
                "字段映射": row.get("字段映射"),
            }
            for row in store.rows(SCHEMA_MODULE)
            if str(row.get("关联服务", "")) == service
        ]
        return {
            "模板编号": entry.get("模板编号", ""),
            "关联服务": service,
            "状态": entry.get("status", ""),
            "输入字段": entry.get("输入字段", ""),
            "输出字段": entry.get("输出字段", ""),
            "格式转换": entry.get("格式转换", ""),
            "默认填充": entry.get("默认填充", ""),
            "数组展开": entry.get("数组展开", ""),
            "嵌套打平": entry.get("嵌套打平", ""),
            "契约版本": entry.get("契约版本", 0),
            "最近输出列": entry.get("最近输出列", ""),
            "接口文档": docs,
        }, "ok"
