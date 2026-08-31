import asyncio
import json
import unittest
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


class BindingCodeServiceTests(unittest.TestCase):
    def test_only_empty_codes_are_generated_and_persisted(self):
        class FakeStore:
            def __init__(self):
                self.records = [
                    {"record_id": "rec_existing", "fields": {"姓名": "已有", "绑定码": "ZZB-EXISTING"}},
                    {"record_id": "rec_empty", "fields": {"姓名": "待绑定", "身份": "导师"}},
                ]
                self.updated = None

            def list_records(self, _table):
                return self.records

            def batch_update(self, _table, updates):
                self.updated = updates

        store = FakeStore()
        generated = generate_binding_codes(store)

        self.assertEqual(len(generated), 1)
        self.assertEqual(generated[0]["name"], "待绑定")
        self.assertEqual(generated[0]["identity"], "导师")
        self.assertRegex(generated[0]["code"], r"^ZZB-[A-Z2-9]{8}$")
        self.assertEqual(store.updated[0]["record_id"], "rec_empty")


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
