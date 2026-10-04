import datetime
import os
import pathlib
import tempfile
import types
import unittest
from unittest import mock

import editor
import preysave


def attr(name, value, type_=preysave.T_INT32):
    return preysave.Attr(name, type_, value)


class FixedDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 3, 12, 34, 56, 123456, tzinfo=tz)


class RegressionTests(unittest.TestCase):
    def test_backups_do_not_collide_at_the_same_timestamp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            save_root = root / "saves"
            backup_root = root / "backups"
            target = save_root / "Campaign0" / "manual0" / "save.CSF"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"original")

            with mock.patch.object(editor, "SAVE_ROOT", str(save_root)), \
                    mock.patch.object(editor, "BACKUP_ROOT", str(backup_root)), \
                    mock.patch.object(editor.datetime, "datetime", FixedDatetime):
                first = editor.backup_file(str(target))
                target.write_bytes(b"edited")
                second = editor.backup_file(str(target))

            self.assertNotEqual(first, second)
            self.assertEqual(pathlib.Path(first).read_bytes(), b"original")
            self.assertEqual(pathlib.Path(second).read_bytes(), b"edited")

    def test_signal_lookup_does_not_create_nodes_in_read_only_mode(self):
        root = preysave.Node("Root")
        game = root.add_child(preysave.Node("IGame"))
        signals = game.add_child(preysave.Node("outboundModifiers"))
        item = signals.add_child(preysave.Node("i"))
        value = item.add_child(preysave.Node("v", [attr("entity", 7)]))

        model = editor.PreyModel.__new__(editor.PreyModel)
        model.save = types.SimpleNamespace(root=root)
        model.player_entity_id = lambda: 7

        self.assertIsNone(model._signal_list(False, False))
        self.assertIsNone(value.child("modifiers"))
        self.assertIsNotNone(model._signal_list(False, True))

    def test_partial_perk_effects_are_detected_and_repaired(self):
        stat_a = preysave.Node("stat", [attr("baseValue", 10), attr("currentValue", 11)])
        mods_a = stat_a.add_child(preysave.Node("modifiers"))
        item_a = mods_a.add_child(preysave.Node("i"))
        value_a = item_a.add_child(preysave.Node("v"))
        value_a.add_child(preysave.Node("Modifier", [attr("id", 1), attr("value", 1)]))
        stat_b = preysave.Node("stat", [attr("baseValue", 20), attr("currentValue", 20)])

        research = preysave.Node("ArkAbilityResearch", [attr("acquired", 1)])
        owned = research.add_child(preysave.Node("modifiers"))
        owned.add_child(preysave.Node("i", [attr("v", 1)]))
        info = {"stats": [("a", 1), ("b", 2)], "signals": []}

        model = editor.PreyModel.__new__(editor.PreyModel)
        stats = {"a": stat_a, "b": stat_b}
        model._stat_by_name = lambda name: stats.get(name)
        model._next_modifier_id = lambda: 2
        model._sync_max_health = lambda: None

        self.assertTrue(model.missing_effects(research, info))
        model.apply_effects(research, info)
        self.assertFalse(model.missing_effects(research, info))
        self.assertEqual(stat_a.get("currentValue"), 11)
        self.assertEqual(stat_b.get("currentValue"), 22)
        self.assertEqual(len(model._list_items(owned)), 2)

    def test_atomic_save_keeps_original_if_replace_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            target = pathlib.Path(temp) / "save.CSF"
            target.write_bytes(b"original")
            dummy = types.SimpleNamespace(path=str(target), to_bytes=lambda: b"replacement")
            save_method = preysave.SaveFile.save

            with mock.patch.object(preysave, "SaveFile", return_value=None), \
                    mock.patch.object(preysave.os, "replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    save_method(dummy)

            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(os.listdir(temp), ["save.CSF"])

    def test_stale_load_completion_is_ignored(self):
        app = editor.EditorApp.__new__(editor.EditorApp)
        app._load_request_id = 2
        app.config = lambda **kwargs: self.fail("stale callback reached the UI")
        app._loaded(1, "old-save.CSF", None, RuntimeError("old failure"))


if __name__ == "__main__":
    unittest.main()
