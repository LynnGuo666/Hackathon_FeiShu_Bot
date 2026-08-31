"""多维表格（选手数据库 / 报名表）读写封装。

两种传输后端：
- sdk：lark-oapi（bot 身份，需 FEISHU_APP_ID/SECRET，且应用需有 bitable:app 权限并被加为 Base 协作者）
- cli：本机 lark-cli（默认；无需应用凭证，走用户身份）

通过环境变量 BASE_BACKEND=sdk|cli 切换，默认 sdk（bot 身份）。
"""
from __future__ import annotations

import json
import os
import subprocess

from .config import CFG

CLI_ENV = {"LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1", "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}


def _cli(*args: str) -> dict:
    """调用 lark-cli 并返回成功信封的 data 部分；失败抛 RuntimeError。"""
    env = {**os.environ, **CLI_ENV}
    proc = subprocess.run(["lark-cli", *args, "--format", "json"], capture_output=True, text=True, env=env)
    try:
        out = json.loads(proc.stdout or proc.stderr)
    except json.JSONDecodeError:
        raise RuntimeError(f"lark-cli 输出解析失败: {proc.stdout[:200]} {proc.stderr[:200]}")
    if not out.get("ok"):
        err = out.get("error", {})
        raise RuntimeError(f"lark-cli 失败: {err.get('message') or err}")
    return out.get("data", {})


def _columnar_to_records(data: dict) -> list[dict]:
    """CLI record-list 的列式返回 {fields:[...], record_id_list:[...], data:[[cell],...]} 转行式。"""
    names = data.get("fields") or []
    rids = data.get("record_id_list") or []
    rows = data.get("data") or []
    out = []
    for rid, row in zip(rids, rows):
        out.append({"record_id": rid,
                    "fields": {name: row[i] for i, name in enumerate(names) if i < len(row) and row[i] is not None}})
    return out


class BaseStore:
    """按表名读写的通用封装。所有分页/分批在此处处理。"""

    _table_id_cache: dict[str, str] = {}  # base_token -> {表名: table_id}

    def __init__(self, base_token: str):
        self.base_token = base_token
        self.backend = "sdk" if (CFG.has_app_credentials and _env_backend() == "sdk") else "cli"

    def resolve_table_id(self, table: str) -> str:
        """SDK 后端只认 table_id；中文表名先解析（带缓存）。CLI 后端两者都支持。"""
        if table.startswith("tbl") or self.backend == "cli":
            return table
        cache = BaseStore._table_id_cache.setdefault(self.base_token, {})
        if table not in cache:
            from lark_oapi.api.bitable.v1 import ListAppTableRequest

            resp = _sdk_client().bitable.v1.app_table.list(
                ListAppTableRequest.builder().app_token(self.base_token).page_size(100).build())
            if not resp.success():
                raise RuntimeError(f"解析表名失败: {resp.code} {resp.msg}")
            for t in (resp.data.items or []):
                cache[t.name] = t.table_id
            if table not in cache:
                raise RuntimeError(f"Base 中不存在表: {table}")
        return cache[table]

    # ---------- 查询 ----------
    def list_records(self, table: str) -> list[dict]:
        """返回全部记录 [{record_id, fields}]，自动翻页。"""
        if self.backend == "cli":
            records, offset = [], 0
            while True:
                data = _cli("base", "+record-list", "--base-token", self.base_token, "--table-id", table,
                            "--limit", "200", "--offset", str(offset))
                page = _columnar_to_records(data)
                records.extend(page)
                if not data.get("has_more") or not page:
                    break
                offset += len(page)
            return records
        return self._sdk_list(table)

    def find(self, table: str, field: str, value: str) -> dict | None:
        for r in self.list_records(table):
            if str((r.get("fields") or {}).get(field, "")).strip() == str(value).strip():
                return r
        return None

    # ---------- 写入 ----------
    def batch_create(self, table: str, rows: list[dict]) -> list[dict]:
        """批量新建，单批 200，返回新建记录（含 record_id）。rows 是字段映射列表。"""
        created = []
        for i in range(0, len(rows), 200):
            chunk = rows[i:i + 200]
            if self.backend == "cli":
                data = _cli("base", "+record-batch-create", "--base-token", self.base_token, "--table-id", table,
                            "--json", json.dumps({"create_records": chunk}, ensure_ascii=False))
                recs = data.get("records") or data.get("items") or []
                created.extend(recs if recs and isinstance(recs[0], dict) and "record_id" in recs[0] else [])
            else:
                created.extend(self._sdk_batch_create(table, chunk))
        return created

    def batch_update(self, table: str, items: list[dict]) -> None:
        """items: [{record_id, fields}]"""
        for i in range(0, len(items), 200):
            chunk = items[i:i + 200]
            if self.backend == "cli":
                payload = {"update_records": {it["record_id"]: it["fields"] for it in chunk}}
                _cli("base", "+record-batch-update", "--base-token", self.base_token, "--table-id", table,
                     "--json", json.dumps(payload, ensure_ascii=False))
            else:
                self._sdk_batch_update(table, chunk)

    # ---------- SDK 后端 ----------
    def _sdk_list(self, table: str) -> list[dict]:
        from lark_oapi.api.bitable.v1 import ListAppTableRecordRequest

        client = _sdk_client()
        records, page_token = [], ""
        while True:
            b = (ListAppTableRecordRequest.builder()
                 .app_token(self.base_token).table_id(self.resolve_table_id(table)).page_size(500)
                 .user_id_type("open_id"))
            if page_token:
                b = b.page_token(page_token)
            resp = client.bitable.v1.app_table_record.list(b.build())
            if not resp.success():
                raise RuntimeError(f"SDK 读取失败: {resp.code} {resp.msg}")
            records.extend({"record_id": it.record_id, "fields": it.fields} for it in (resp.data.items or []))
            if not resp.data.has_more:
                break
            page_token = resp.data.page_token
        return records

    def _sdk_batch_create(self, table: str, rows: list[dict]) -> list[dict]:
        from lark_oapi.api.bitable.v1 import (BatchCreateAppTableRecordRequest,
                                              BatchCreateAppTableRecordRequestBody)

        client = _sdk_client()
        body = BatchCreateAppTableRecordRequestBody.builder()
        body.records([{"fields": _to_sdk_fields(r)} for r in rows])
        req = (BatchCreateAppTableRecordRequest.builder()
               .app_token(self.base_token)
               .table_id(self.resolve_table_id(table))
               .request_body(body.build()))
        resp = client.bitable.v1.app_table_record.batch_create(req.build())
        if not resp.success():
            raise RuntimeError(f"SDK 批量新建失败: {resp.code} {resp.msg}")
        return [{"record_id": it.record_id, "fields": it.fields} for it in (resp.data.records or [])]

    def _sdk_batch_update(self, table: str, items: list[dict]) -> None:
        from lark_oapi.api.bitable.v1 import (BatchUpdateAppTableRecordRequest,
                                              BatchUpdateAppTableRecordRequestBody)

        client = _sdk_client()
        body = BatchUpdateAppTableRecordRequestBody.builder()
        body.records([{"record_id": it["record_id"], "fields": _to_sdk_fields(it["fields"])} for it in items])
        req = (BatchUpdateAppTableRecordRequest.builder()
               .app_token(self.base_token)
               .table_id(self.resolve_table_id(table))
               .request_body(body.build()))
        resp = client.bitable.v1.app_table_record.batch_update(req.build())
        if not resp.success():
            raise RuntimeError(f"SDK 批量更新失败: {resp.code} {resp.msg}")


def _env_backend() -> str:
    """默认 sdk（bot 身份，生产路径）；cli 仅用于本地调试（依赖 lark-cli 登录态）。"""
    return os.environ.get("BASE_BACKEND", "sdk").strip().lower()


def _to_sdk_fields(fields: dict) -> dict:
    """CLI 形态字段值 -> SDK 形态。link 字段：[{"id":...}] -> ["rec..."]（SingleLink 只接受 record_id 字符串列表）。"""
    out = {}
    for k, v in fields.items():
        if isinstance(v, list) and v and isinstance(v[0], dict) and "id" in v[0]:
            out[k] = [x["id"] for x in v if isinstance(x, dict) and x.get("id")]
        else:
            out[k] = v
    return out


_SDK_CLIENT = None


def _sdk_client():
    global _SDK_CLIENT
    if _SDK_CLIENT is None:
        import lark_oapi as lark
        _SDK_CLIENT = lark.Client.builder().app_id(CFG.app_id).app_secret(CFG.app_secret).build()
    return _SDK_CLIENT
