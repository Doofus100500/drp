import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import drp_home as drp

PROFILE = json.loads((Path(__file__).resolve().parents[1] / "config/access.example.json").read_text())


class AccessProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "access.json"

    def test_payload_and_full_unwrap_response_are_accepted_without_gitlab_password(self):
        for data in (PROFILE, {"request_id": "fixture", "data": PROFILE}):
            self.path.write_text(json.dumps(data))
            result = drp.read_access_profile(self.path)
            self.assertEqual(result, {key: PROFILE[key] for key in drp.ACCESS_FIELDS})
            self.assertNotIn("password", result)

    def test_incomplete_or_invalid_profile_fails_without_exposing_values(self):
        invalid = [[], None, {"password": "private-secret"}, {"data": {"password": "private-secret"}}]
        for key in drp.ACCESS_FIELDS:
            invalid.extend([{**PROFILE, key: None}, {**PROFILE, key: "private-secret\n"}])
        for repo in ("/local", "s3:http://s3.example.org/bucket", "s3:https://user:private-secret@s3.example.org/bucket",
                     "s3:https://s3.example.org/bucket?token=private-secret", "s3:https://s3.example.org",
                     "s3:https://s3.example.org:bad/bucket"):
            invalid.append({**PROFILE, "repository": repo})
        invalid.append({**PROFILE, "environment": "production,another"})
        for data in invalid:
            with self.subTest(data_type=type(data).__name__):
                self.path.write_text(json.dumps(data))
                with self.assertRaises(drp.Failure) as raised:
                    drp.read_access_profile(self.path)
                self.assertNotIn("private-secret", str(raised.exception))

    def test_bad_json_does_not_leak_input(self):
        self.path.write_text('{"private-secret":')
        with self.assertRaises(drp.Failure) as raised:
            drp.read_access_profile(self.path)
        self.assertNotIn("private-secret", str(raised.exception))

    def configure(self, profile=PROFILE):
        self.path.write_text(json.dumps(profile))
        with patch.object(drp, "BASE", self.root / "home"), patch.object(drp, "ETC", self.root / "etc"):
            with patch("builtins.input", side_effect=["git.example.org", "direct", "admin@example.org"]):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    drp.configure(self.path)
        return output.getvalue()

    def test_setup_installs_only_needed_fields_with_private_permissions(self):
        output = self.configure()
        etc = self.root / "etc"
        config = drp.load_json(etc / "config.json")
        self.assertEqual({key: config[key] for key in drp.BACKUP_FIELDS},
                         {key: PROFILE[key] for key in drp.BACKUP_FIELDS})
        self.assertEqual(drp.load_json(etc / "s3.json"), {
            "AWS_ACCESS_KEY_ID": PROFILE["s3_access_key"], "AWS_SECRET_ACCESS_KEY": PROFILE["s3_secret_key"]})
        self.assertEqual((etc / "restic-password").read_text(), PROFILE["restic_password"] + "\n")
        for path in etc.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(PROFILE["password"], path.read_text())
        for key in ("s3_access_key", "s3_secret_key", "restic_password"):
            self.assertNotIn(key, config)
            self.assertNotIn(PROFILE[key], output)
        self.assertEqual(etc.stat().st_mode & 0o777, 0o700)

    def test_missing_field_does_not_mark_setup_complete(self):
        with self.assertRaises(drp.Failure):
            self.configure({"password": "fixture"})
        self.assertFalse((self.root / "etc/config.json").exists())
        self.assertFalse((self.root / "home/.managed").exists())

    def test_rerun_preserves_configuration_without_requiring_original_profile(self):
        self.configure()
        self.path.unlink()
        before = (self.root / "etc/config.json").read_bytes()
        with patch.object(drp, "BASE", self.root / "home"), patch.object(drp, "ETC", self.root / "etc"):
            with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                with contextlib.redirect_stdout(io.StringIO()):
                    drp.configure(self.path)
        self.assertEqual((self.root / "etc/config.json").read_bytes(), before)

    def test_interactive_retries_invalid_answers_without_restarting_setup(self):
        self.path.write_text(json.dumps(PROFILE))
        answers = [str(self.root / "missing.json"), str(self.path),
                   "https://git.example.org", "git.example.org", "invalid", "1", "bad-email", "admin@example.org"]
        with patch.object(drp, "BASE", self.root / "home"), patch.object(drp, "ETC", self.root / "etc"), \
                patch("builtins.input", side_effect=answers) as prompts, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            drp.configure()
        config = drp.load_json(self.root / "etc/config.json")
        self.assertEqual((config["domain"], config["mode"], config["email"]),
                         ("git.example.org", "direct", "admin@example.org"))
        self.assertEqual(prompts.call_count, len(answers))
        for secret in (PROFILE["password"], PROFILE["s3_secret_key"], PROFILE["restic_password"]):
            self.assertNotIn(secret, output.getvalue())

    def test_proxy_choice_retries_ip_and_saves_instructions_for_selected_domain(self):
        self.path.write_text(json.dumps(PROFILE))
        answers = ["git.example.org", "2", "invalid-address", "192.168.1.10"]
        with patch.object(drp, "BASE", self.root / "home"), patch.object(drp, "ETC", self.root / "etc"), \
                patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
            drp.configure(self.path)
        etc = self.root / "etc"
        config = drp.load_json(etc / "config.json")
        self.assertEqual(config["mode"], "proxy")
        self.assertEqual(config["bind_ip"], "192.168.1.10")
        guide = (etc / "nginx-setup.txt").read_text()
        self.assertIn("https://git.example.org", guide)
        self.assertIn("http://192.168.1.10:5080", guide)
        self.assertIn("--cert-name git.example.org", guide)
        self.assertIn("Persistent=true", guide)
        self.assertNotIn(PROFILE["s3_secret_key"], guide)
        self.assertEqual((etc / "nginx-setup.txt").stat().st_mode & 0o777, 0o600)

    def test_end_of_input_stops_without_saving_configuration(self):
        with patch.object(drp, "BASE", self.root / "home"), patch.object(drp, "ETC", self.root / "etc"), \
                patch("builtins.input", side_effect=EOFError), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(drp.Failure, "терминале"):
                drp.configure()
        self.assertFalse((self.root / "etc/config.json").exists())

    def test_user_password_only_profile_explains_admin_bundle_requirement(self):
        for data in ({"password": "private-secret"}, {"data": {"password": "private-secret"}}):
            self.path.write_text(json.dumps(data))
            with self.assertRaisesRegex(drp.Failure, "admins") as error:
                drp.read_access_profile(self.path)
            self.assertNotIn("private-secret", str(error.exception))

    def test_final_instructions_use_home_addresses_without_printing_credentials(self):
        self.configure()
        config = drp.load_json(self.root / "etc/config.json")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            drp.access_instructions(config)
        self.assertIn("https://git.example.org", output.getvalue())
        self.assertIn("git.example.org:5050", output.getvalue())
        for secret in (PROFILE["password"], PROFILE["s3_secret_key"], PROFILE["restic_password"]):
            self.assertNotIn(secret, output.getvalue())


if __name__ == "__main__":
    unittest.main()
