#!/usr/bin/env python3
"""一次性脚本：把指定的人设为所有选手群的群管理员。

默认名单：贾国龙 / cran / 谭博午 / 李宇杰（可用命令行参数覆盖）。

找人优先级：
1. 组委会表按姓名精确匹配 → open_id；
2. 选手表按姓名精确匹配 → open_id；
3. 表里有记录但没 open_id 且有手机号 → 手机号反查 open_id；
4. 仍查不到 → 在各选手群成员里按群昵称不区分大小写匹配（如英文昵称 "cran"）。

用法：
  .venv/bin/python scripts/add_group_managers.py           # dry-run，只打印计划
  .venv/bin/python scripts/add_group_managers.py --run     # 真正执行
  .venv/bin/python scripts/add_group_managers.py 姓名1 姓名2
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from bot.core.lark_client import (add_group_managers, add_members,  # noqa: E402
                                  list_members, resolve_open_ids_by_phones)
from bot.core.service import SVC, init_services  # noqa: E402
from bot.modules.group.group import ID_ALL, ID_CONTESTANT  # noqa: E402

DEFAULT_NAMES = ["贾国龙", "cran", "谭博午", "李宇杰"]


async def resolve_users(names: list[str], chat_ids: list[str]) -> dict[str, tuple[str, str]]:
    """把姓名解析成 open_id，返回 {姓名: (open_id, 来源)}；查不到的不在结果里。"""
    organizers = {o.name: o for o in await SVC.organizers.list_all()}
    contestants = {c.name: c for c in await SVC.contestants.list_all()}

    resolved: dict[str, tuple[str, str]] = {}
    pending: list[str] = []
    for name in names:
        org = organizers.get(name)
        if org and org.open_id:
            resolved[name] = (org.open_id, f"组委会表({org.identity or '主办方'})")
            continue
        con = contestants.get(name)
        if con and con.open_id:
            resolved[name] = (con.open_id, "选手表")
            continue
        rec = org or con
        if rec and rec.phone:
            pending.append(name)  # 留给手机号反查
        else:
            pending.append(name)  # 留给群昵称兜底

    if any(organizers.get(n) or contestants.get(n) for n in pending):
        phones = [(organizers.get(n) or contestants.get(n)).phone for n in pending
                  if (organizers.get(n) or contestants.get(n)) and
                  (organizers.get(n) or contestants.get(n)).phone]
        if phones:
            by_phone = await asyncio.to_thread(resolve_open_ids_by_phones, phones)
            for name in list(pending):
                rec = organizers.get(name) or contestants.get(name)
                if rec and rec.phone:
                    open_id = by_phone.get(rec.phone.lstrip("+"))
                    if open_id:
                        resolved[name] = (open_id, "手机号反查")
                        pending.remove(name)

    # 兜底：在各选手群成员里按群昵称匹配
    if pending:
        name_lowers = {n.lower(): n for n in pending}
        for chat_id in chat_ids:
            try:
                members = await asyncio.to_thread(list_members, chat_id)
            except Exception as exc:
                print(f"  [警告] 读群成员失败 chat_id={chat_id}: {exc}")
                continue
            for open_id, nickname in members.items():
                nl = (nickname or "").lower()
                hit = name_lowers.get(nl) or next(
                    (n for low, n in name_lowers.items() if low and low in nl), None)
                if hit and hit not in resolved:
                    resolved[hit] = (open_id, f"群昵称「{nickname}」")
        for name in pending:
            if name not in resolved:
                print(f"  [未匹配] {name}: 组委会表/选手表/各群群昵称均未找到")

    return resolved


def pick_contestant_chats(configs) -> list[tuple[str, str]]:
    out = []
    for cfg in configs:
        if not (cfg.enabled and cfg.chat_id):
            continue
        aud = cfg.audiences or [ID_CONTESTANT]
        if ID_ALL in aud or ID_CONTESTANT in aud:
            out.append((cfg.name, cfg.chat_id))
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description="给所有选手群批量设置群管理员")
    parser.add_argument("names", nargs="*", default=[], help="要设为管理员的姓名（默认内置名单）")
    parser.add_argument("--run", action="store_true", help="真正执行（默认 dry-run）")
    args = parser.parse_args()
    names = args.names or DEFAULT_NAMES

    init_services()
    chats = pick_contestant_chats(await SVC.groups.list_configs())
    print(f"目标名单: {names}")
    print(f"目标群: {len(chats)} 个")
    for gname, chat_id in chats:
        print(f"  - {gname} ({chat_id})")

    resolved = await resolve_users(names, [c for _, c in chats])
    if not resolved:
        print("没有任何人被解析到 open_id，退出。")
        return
    print("\n解析结果:")
    open_ids: dict[str, str] = {}
    for name in names:
        if name in resolved:
            open_ids[name] = resolved[name][0]
            print(f"  {name} -> {resolved[name][0]}  [{resolved[name][1]}]")
        else:
            print(f"  {name} -> 未解析，跳过")

    print()
    results: list[tuple[str, str]] = []
    for gname, chat_id in chats:
        ids = list(open_ids.values())
        if not ids:
            continue
        try:
            members = await asyncio.to_thread(list_members, chat_id)
        except Exception as exc:
            results.append((gname, f"读群成员失败: {exc}"))
            continue
        missing = [oid for oid in ids if oid not in members]
        actions = []
        if missing:
            actions.append(f"先拉入 {len(missing)} 人")
        if not args.run:
            results.append((gname, ("计划: " + "；".join(actions) + "；设管理员")
                            if actions else "计划: 直接设管理员"))
            continue
        pull_err = ""
        if missing:
            _, pull_err = await asyncio.to_thread(add_members, chat_id, missing)
            actions.append(f"已拉入 {len(missing)} 人")
            if pull_err:
                actions.append(f"拉入警告: {pull_err}")
        try:
            await asyncio.to_thread(add_group_managers, chat_id, ids)
            actions.append("设管理员成功")
            results.append((gname, "；".join(actions)))
        except Exception as exc:
            results.append((gname, f"设管理员失败: {exc}"))

    print("执行结果:" if args.run else "计划预览:")
    for gname, status in results:
        print(f"  - {gname}: {status}")


if __name__ == "__main__":
    asyncio.run(main())
