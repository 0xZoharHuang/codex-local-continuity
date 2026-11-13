import contextlib
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "src"))
import doctor


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.codex = self.root / "native"
        self.data = self.root / "helper/data"
        self.codex.mkdir()
        self.profile("person-a","team-a",["target-secret"])
        self.config = self.codex / "config.toml"
        self.config.write_text('[plugins."private-plugin@local"]\nenabled = true\n')
        cache = self.codex / "plugins/cache/local/private-plugin/v1/.codex-plugin"
        cache.mkdir(parents=True)
        (cache/"plugin.json").write_text('{"fixture":"never read this manifest body"}')
        (cache.parent.parent/"current").symlink_to("v1",target_is_directory=True)
        self.registry = self.codex / "sqlite/codex.db"
        self.registry.parent.mkdir()
        with contextlib.closing(sqlite3.connect(self.registry)) as db:
            names = list(doctor.SCHEDULE_COLUMNS) + ["name","prompt","last_run_at"]
            db.execute("CREATE TABLE automations (" + ",".join(c+" TEXT" for c in names) + ")")
            self.add_row(db, id="private-task", status="ACTIVE", kind="heartbeat", rrule="FREQ=DAILY",
                         target_thread_id="target-secret", user_id="person-a", account_id="team-a",
                         name="do not report this title", prompt="never show this prompt", last_run_at="yesterday")
            db.commit()
        task = self.codex / "automations/private-task"
        task.mkdir(parents=True)
        (task/"automation.toml").write_text('prompt = "never read this file body"')
        self.index = self.codex / "state_5.sqlite"
        with contextlib.closing(sqlite3.connect(self.index)) as db:
            db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO threads VALUES ('target-secret')")
            db.commit()

    @staticmethod
    def add_row(db, **row):
        cols = ",".join(row)
        params = ",".join("?" for _ in row)
        db.execute(f"INSERT INTO automations ({cols}) VALUES ({params})", tuple(row.values()))

    def profile(self,user,account,targets):
        encoded = json.dumps([user,account],separators=(",",":")).encode()
        state = {"electron-account-renderer-state-v1":{"owner":hashlib.sha256(encoded).hexdigest(),"inactive":{}},
                 "electron-persisted-atom-state":{
                     "local-document-identity":{"userId":user,"accountId":account},
                     "heartbeat-thread-permissions-by-id":{t:"do not expose this policy" for t in targets},
                     "composer-prompt-drafts-v2":{"one":"never show this prompt"},
                 }}
        (self.codex/".codex-global-state.json").write_text(json.dumps(state))

    def capture(self,mode="show",**kwargs):
        return doctor.run(mode,data=self.data,codex=self.codex,**kwargs)

    def test_read_only_show_and_private_baseline(self):
        report = self.capture()
        self.assertFalse(self.data.parent.exists())
        self.assertEqual(report["plugins"]["with_cached_manifest"],1)
        self.assertEqual(report["plugins"]["explicitly_enabled"],1)
        self.assertEqual(report["schedules"]["active"],1)
        self.assertEqual(report["schedules"]["indexed_targets"],1)
        self.assertIn("task-execution",report["not_observed"])
        self.capture("baseline")
        file = self.data / doctor.BASELINE
        self.assertEqual(stat.S_IMODE(file.stat().st_mode),0o600)
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode),0o700)
        data = file.read_text()
        for text in ["person-a","team-a","target-secret","private-task","private-plugin",
                     "never show this prompt","do not report this title","never read this file body",
                     "do not expose this policy","FREQ=DAILY",str(self.codex)]:
            self.assertNotIn(text, data)
            self.assertNotIn(text, json.dumps(report))
        with self.assertRaises(FileExistsError):
            self.capture("baseline")
        self.assertEqual(file.read_text(),data)
        self.capture("baseline",replace_baseline=True)
        self.assertNotEqual(file.read_text(),data)
        self.assertFalse(list(self.codex.rglob("*-shm")))
        self.assertFalse(list(self.codex.rglob("*-wal")))

    def test_compare_owner_status_target_definition_and_permissions(self):
        self.capture("baseline")
        self.assertEqual(self.capture("compare")["comparison"]["active_profile"],"same")
        self.profile("person-b","team-b",[])
        self.config.write_text('[plugins."private-plugin@local"]\nenabled = false\n')
        with contextlib.closing(sqlite3.connect(self.registry)) as db:
            db.execute("UPDATE automations SET status='PAUSED', target_thread_id='other-target'")
            db.execute("UPDATE automations SET prompt='other unsafe body', last_run_at='today'")
            db.commit()
        changes = self.capture("compare")
        c = changes["comparison"]
        self.assertEqual(c["active_profile"],"different")
        self.assertEqual(c["plugins"]["changed_by_field"],{"enablement":1})
        self.assertEqual(c["schedules"]["changed_by_field"],{"status":1,"target":1})
        self.assertEqual(c["schedule_checks"]["indexed_targets"]["removed"],1)
        self.assertEqual(c["schedule_checks"]["permission_cache_targets"]["removed"],1)
        self.assertEqual(changes["schedules"]["owner_binding"]["other_profile"],1)
        self.assertEqual(changes["schedules"]["missing_targets"],1)
        self.assertEqual(changes["schedules"]["paused"],1)
        for lang, fragments in (("en",("different","1 paused","target 1","authorization")),
                                ("zh",("身份不同","暂停 1","目标 1","外部授权"))):
            text = doctor.format_report(changes,lang)
            for fragment in fragments:
                self.assertIn(fragment,text)
            self.assertNotIn("target-secret",text)

    def test_missing_unknown_and_empty_are_distinct(self):
        self.config.unlink()
        self.index.unlink()
        self.assertEqual(self.capture()["plugins"]["state"],"unknown")
        self.assertEqual(self.capture()["schedules"]["local_thread_target_index"],"unknown")
        self.config.write_text("")
        with contextlib.closing(sqlite3.connect(self.registry)) as db:
            db.execute("DELETE FROM automations")
            db.commit()
        report = self.capture()
        self.assertEqual(report["plugins"]["configured"],0)
        self.assertEqual(report["schedules"]["registered"],0)
        self.assertEqual(report["schedules"]["files_without_registry"],1)
        self.capture("baseline")
        self.config.write_text('instructions = """\n[plugins."pretend@entry"]\nenabled = true\n"""\n')
        self.assertEqual(self.capture("compare")["comparison"]["plugins"]["state"],"unknown")
        self.assertEqual(self.capture("compare")["comparison"]["schedules"]["state"],"compared")

    def test_identity_switch_during_one_snapshot_is_not_attributed(self):
        self.capture("baseline")
        collect = doctor.schedules
        def switch_during_read(*args):
            result = collect(*args)
            self.profile("person-b","team-b",[])
            return result
        with mock.patch.object(doctor, "schedules", side_effect=switch_during_read):
            report = self.capture("compare")
        self.assertEqual(report["profile"]["reason"],"identity-changed-during-snapshot")
        self.assertEqual(report["comparison"]["active_profile"],"unknown")
        self.assertEqual(report["comparison"]["plugins"]["changed"],0)
        self.assertEqual(report["comparison"]["schedules"]["changed"],0)
        self.assertEqual(report["schedules"]["active"],1)
        self.assertEqual(report["schedules"]["indexed_targets"],1)
        self.assertIsNone(report["schedules"]["owner_binding"]["active_profile"])
        self.assertEqual(report["schedules"]["owner_binding"]["unknown"],1)
        self.assertIsNone(report["schedules"]["targets_with_active_profile_permission_cache"])
        self.assertEqual(report["comparison"]["schedule_checks"]["permission_cache_targets"]["state"],"unknown")

    def test_quoted_plugin_tables_are_recognized_or_explicitly_unknown(self):
        for header in ('["plugins"."private-plugin@local"]', "['plugins'.'private-plugin@local']",
                       r'["pl\u0075gins"."private-plugin@local"]'):
            with self.subTest(header=header):
                self.config.write_text(header+'\n"enabled" = true\n')
                self.assertEqual(self.capture()["plugins"]["explicitly_enabled"],1)
        for syntax in ('["plugins"]\n"private-plugin@local" = { enabled = true }',
                       "'plugins'.'private-plugin@local'.enabled = true",
                       '[["plugins"."private-plugin@local"]]'):
            with self.subTest(syntax=syntax):
                self.config.write_text(syntax)
                self.assertEqual(self.capture()["plugins"]["state"],"unknown")

    def test_schema_change_unsigned_owner_and_unindexed_wal(self):
        self.capture("baseline")
        self.registry.with_name("codex.db-wal").write_bytes(b"unsupported-wal")
        self.assertEqual(self.capture()["schedules"]["state"],"unknown")
        self.assertFalse(self.registry.with_name("codex.db-shm").exists())
        self.registry.with_name("codex.db-wal").unlink()
        with contextlib.closing(sqlite3.connect(self.registry)) as db:
            db.execute("ALTER TABLE automations RENAME COLUMN status TO future_status")
            db.commit()
        (self.codex/".codex-global-state.json").write_text(json.dumps({
            "electron-account-renderer-state-v1":{"owner":"not-a-verified-owner"}}))
        report = self.capture("compare")
        self.assertEqual(report["comparison"]["active_profile"],"unknown")
        self.assertEqual(report["comparison"]["schedules"]["state"],"unknown")
        self.assertEqual(report["comparison"]["plugins"]["state"],"compared")
        self.assertIn("registry unknown",doctor.format_report(report,"en"))
        self.assertEqual(report["schedules"]["definition_files"],1)

    def test_no_implicit_replacement_or_native_writes_when_baseline_unsafe(self):
        self.capture("baseline")
        original = self.config.read_bytes()
        baseline = self.data/doctor.BASELINE
        baseline.chmod(0o644)
        with self.assertRaises(ValueError):
            self.capture("compare")
        with self.assertRaises(ValueError):
            self.capture("baseline",replace_baseline=True)
        baseline.unlink()
        baseline.symlink_to(self.config)
        with self.assertRaises(ValueError):
            self.capture("baseline",replace_baseline=True)
        self.assertEqual(self.config.read_bytes(),original)


if __name__ == "__main__":
    unittest.main()
