import asyncio
import json
import unittest
import types
import unittest.mock
from unittest.mock import patch

from bot.core.registry import Registry
from bot.modules.auth.binding_codes import generate_binding_codes


class RegistryScopeTests(unittest.TestCase):
    def test_command_and_card_scopes_are_explicit(self):
        registry = Registry()

        async def handler(_ctx):
            return None

        registry.user_command("用户")(handler)
        registry.admin_command("管理员")(handler)
        registry.on_card("用户卡片", scope="user")(handler)
        registry.on_card("管理员卡片", scope="admin")(handler)

        self.assertEqual(registry.command_scopes["用户"], "user")
        self.assertEqual(registry.command_scopes["管理员"], "admin")
        self.assertTrue(registry.allows_command("用户", False))
        self.assertFalse(registry.allows_command("管理员", False))
        self.assertTrue(registry.allows_command("管理员", True))
        self.assertFalse(registry.allows_card("管理员卡片", False))
        self.assertTrue(registry.allows_card("管理员卡片", True))


class FakeOrganizerRepo:
    """duck-typing 假实现：与 FeishuOrganizerRepo 同一方法契约。"""

    def __init__(self, organizers):
        self.organizers = organizers
        self.updates = []

    async def get_by_open_id(self, open_id):
        return next((o for o in self.organizers if o.open_id == open_id), None)

    async def get_by_binding_code(self, code):
        return next((o for o in self.organizers if o.binding_code == code), None)

    async def list_all(self):
        return self.organizers

    async def update(self, record_id, **fields):
        self.updates.append((record_id, fields))

    async def batch_update(self, items):
        self.updates.extend(items)


class BindingCodeServiceTests(unittest.TestCase):
    def test_only_empty_codes_are_generated_and_persisted(self):
        from bot.core.models import Organizer
        from bot.core.service import SVC

        repo = FakeOrganizerRepo([
            Organizer(record_id="rec_existing", name="已有", binding_code="ZZB-EXISTING"),
            Organizer(record_id="rec_empty", name="待绑定", identity="导师"),
        ])
        generated = asyncio.run(self._generate(repo))

        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0]["name"], "待绑定")
        self.assertEqual(generated[0]["identity"], "导师")
        self.assertRegex(generated[0]["code"], r"^ZZB-[A-Z2-9]{8}$")
        self.assertEqual(repo.updates[0][0], "rec_empty")
        self.assertEqual(repo.updates[0][1]["binding_code"], generated[0]["code"])

    @staticmethod
    async def _generate(repo):
        from bot.core import service
        old = service.SVC.organizers
        service.SVC.organizers = repo
        try:
            return await generate_binding_codes()
        finally:
            service.SVC.organizers = old


class EventRoutingTests(unittest.TestCase):
    def test_admin_command_is_blocked_before_handler_for_regular_user(self):
        from bot.core import event_bus

        registry = Registry()
        calls = []

        async def handler(_ctx):
            calls.append("handler")

        registry.admin_command("同步")(handler)
        event = {
            "header": {"event_id": "test-admin-command"},
            "sender": {"sender_id": {"open_id": "ou_regular"}},
            "message": {
                "message_type": "text",
                "chat_type": "p2p",
                "content": json.dumps({"text": "同步"}),
                "message_id": "om_test",
            },
        }

        with patch.object(event_bus, "REGISTRY", registry), \
                patch.object(event_bus, "is_admin", return_value=False), \
                patch("bot.core.lark_client.send_text", new_callable=unittest.mock.AsyncMock) as send_text:
            asyncio.run(event_bus.handle_message(event))

        self.assertEqual(calls, [])
        send_text.assert_awaited_once_with("ou_regular", "该指令仅限管理员使用。")

    def test_p2p_command_is_silently_ignored_in_group(self):
        from bot.core import event_bus

        registry = Registry()
        calls = []

        async def handler(_ctx):
            calls.append("handler")

        async def fallback(_ctx):
            calls.append("fallback")

        registry.user_command("投票")(handler)
        registry.on_fallback(fallback)
        base_event = {
            "sender": {"sender_id": {"open_id": "ou_regular"}},
            "message": {
                "message_type": "text",
                "chat_type": "group",
                "chat_id": "oc_group",
                "message_id": "om_group",
            },
        }
        # 群里发私聊指令：不触发处理器；群里发未知文本：也不回帮助卡片
        for eid, text in (("test-group-cmd", "投票"), ("test-group-unknown", "随便聊聊")):
            event = dict(base_event, header={"event_id": eid})
            event["message"] = dict(base_event["message"],
                                    content=json.dumps({"text": text}))
            with patch.object(event_bus, "REGISTRY", registry), \
                    patch.object(event_bus, "is_admin", return_value=False):
                asyncio.run(event_bus.handle_message(event))

        self.assertEqual(calls, [])

    def test_admin_command_reaches_handler_for_admin(self):
        from bot.core import event_bus

        registry = Registry()
        calls = []

        async def handler(ctx):
            calls.append(ctx.is_admin)

        registry.admin_command("同步")(handler)
        event = {
            "header": {"event_id": "test-admin-command-admin"},
            "sender": {"sender_id": {"open_id": "ou_admin"}},
            "message": {
                "message_type": "text",
                "chat_type": "p2p",
                "content": json.dumps({"text": "同步"}),
                "message_id": "om_test_admin",
            },
        }

        with patch.object(event_bus, "REGISTRY", registry), \
                patch.object(event_bus, "is_admin", return_value=True):
            asyncio.run(event_bus.handle_message(event))

        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
