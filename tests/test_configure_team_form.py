import types
import unittest
from unittest.mock import patch

from scripts import configure_team_form


def response(items):
    return types.SimpleNamespace(
        success=lambda: True,
        data=types.SimpleNamespace(items=items),
    )


class TeamFormConfigurationTests(unittest.TestCase):
    def test_configurator_uses_the_existing_single_select_team_size_field(self):
        """The public configurator exposes the form's existing team-size gate."""
        fields = [
            types.SimpleNamespace(field_name="队长姓名", field_id="captain-name"),
            types.SimpleNamespace(field_name="队长邮箱", field_id="captain-email"),
            types.SimpleNamespace(field_name="队长手机号（自动校验）", field_id="captain-phone"),
            types.SimpleNamespace(field_name="队伍人数", field_id="team-size"),
            types.SimpleNamespace(field_name="队友信息（可选）", field_id="teammate-blob"),
        ]
        form_fields = [
            types.SimpleNamespace(field_id=field.field_id) for field in fields
        ]
        client = types.SimpleNamespace(
            bitable=types.SimpleNamespace(
                v1=types.SimpleNamespace(
                    app_table=types.SimpleNamespace(
                        list=lambda _: response([types.SimpleNamespace(
                            name="组队收集表", table_id="table-1"
                        )])
                    ),
                    app_table_field=types.SimpleNamespace(list=lambda _: response(fields)),
                    app_table_form_field=types.SimpleNamespace(
                        list=lambda _: response(form_fields)
                    ),
                )
            )
        )

        with (
            patch("scripts.configure_team_form._sdk_client", return_value=client),
            patch("scripts.configure_team_form.patch_form_field") as patch_field,
        ):
            configure_team_form.main()

        count_calls = [
            kwargs
            for args, kwargs in patch_field.call_args_list
            if args[2] == "team-size"
        ]
        self.assertEqual(len(count_calls), 1)
        self.assertTrue(count_calls[0]["visible"])
        self.assertTrue(count_calls[0]["required"])


if __name__ == "__main__":
    unittest.main()
