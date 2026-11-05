import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import drp_home as drp

PROFILE = json.loads((Path(__file__).resolve().parents[1] / "config/access.example.json").read_text())
BACKUP_SETTINGS = {key: PROFILE[key] for key in drp.BACKUP_FIELDS}


def fixture(root, version="19.4.3", skipped="tar,uploads,lfs,artifacts,packages"):
    for name in ("config", "gitlab/db", "gitlab/repositories"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "config/gitlab-secrets.json").write_text('{"gitlab_rails": {"secret_key_base": "fixture"}}')
    (root / "gitlab/db/database.sql.gz").write_bytes(b"fixture database")
    (root / "gitlab/registry.tar.gz").write_bytes(b"fixture registry")
    (root / "gitlab/backup_information.yml").write_text(
        ":backup_created_at: !ruby/object:ActiveSupport::TimeWithZone\n"
        "  utc: 2026-10-28 01:00:00 Z\n"
        f":gitlab_version: {version}\n:skipped: {skipped}\n:repositories_server_side: false\n")
    (root / "container.json").write_text(json.dumps({
        "image": f"registry.example.org/gitlab/gitlab-ce:{version}-ce.0",
        "GITLAB_OMNIBUS_CONFIG": "production configuration must not run"}))
    (root / "repositories.json").write_text(json.dumps({"schema_version": 1, "repositories": []}))


def snapshot(char, day=28):
    return {"id": char * 64, "time": f"2026-10-{day}T01:00:00Z",
            "hostname": PROFILE["source_host"], "tags": ["gitlab", "production"],
            "paths": ["/opt/gitlab/data/backups/.restic-staging"]}


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "home"
        self.base.mkdir()
        self.etc = self.root / "etc"
        self.etc.mkdir()
        (self.base / ".managed").write_text("fixture")
        drp.save_json(self.etc / "config.json", {
            **BACKUP_SETTINGS, "domain": "git.example.org", "mode": "direct", "bind_ip": "127.0.0.1"})
        drp.save_json(self.etc / "s3.json", {
            "AWS_ACCESS_KEY_ID": "fixture-access", "AWS_SECRET_ACCESS_KEY": "fixture-secret"})
        (self.etc / "restic-password").write_text("fixture-password")
        self.home = drp.Home(self.base, self.etc)


class BackupTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.backup = self.root / "backup"
        fixture(self.backup)

    def test_accepts_ruby_yaml_and_maps_exact_image_to_public_registry(self):
        info = drp.inspect_backup(self.backup)
        self.assertEqual(info["image"], "docker.io/gitlab/gitlab-ce:19.4.3-ce.0")
        self.assertEqual(info["storages"], ["default"])

    def test_mismatch_and_non_versioned_images_are_rejected(self):
        path = self.backup / "container.json"
        for image in ("gitlab/gitlab-ce:latest", "gitlab/gitlab-ce:19.3.0-ce.0",
                      "gitlab/gitlab-ee:19.4.3-ce.0", "malicious:latest"):
            with self.subTest(image=image):
                drp.save_json(path, {"image": image})
                with self.assertRaises(drp.BadSnapshot):
                    drp.inspect_backup(self.backup)

    def test_restored_components_follow_backup_metadata(self):
        fixture(self.backup, skipped="tar,uploads")
        self.assertEqual(drp.inspect_backup(self.backup)["skipped"], ["tar", "uploads"])
        fixture(self.backup, skipped="tar,registry")
        (self.backup / "gitlab/registry.tar.gz").unlink()
        self.assertIn("registry", drp.inspect_backup(self.backup)["skipped"])
        fixture(self.backup)
        (self.backup / "gitlab/registry.tar.gz").unlink()
        with self.assertRaises(drp.BadSnapshot):
            drp.inspect_backup(self.backup)

    def test_missing_database_and_repositories_skip_fail(self):
        fixture(self.backup, skipped="tar,db")
        with self.assertRaises(drp.BadSnapshot):
            drp.inspect_backup(self.backup)
        fixture(self.backup)
        (self.backup / "gitlab/db/database.sql.gz").unlink()
        with self.assertRaises(drp.BadSnapshot):
            drp.inspect_backup(self.backup)

    def test_symlinked_secrets_are_rejected(self):
        secrets = self.backup / "config/gitlab-secrets.json"
        secrets.unlink()
        secrets.symlink_to(self.etc / "s3.json")
        with self.assertRaises(drp.BadSnapshot):
            drp.inspect_backup(self.backup)

    def test_catalog_storage_names_and_missing_refs_are_checked(self):
        catalog = {"schema_version": 1, "repositories": [{
            "storage": "secondary", "relative_path": "@hashed/a.git", "state": "empty",
            "steps": [{"ref_path": "a.refs"}]}]}
        drp.save_json(self.backup / "repositories.json", catalog)
        with self.assertRaises(drp.BadSnapshot):
            drp.inspect_backup(self.backup)
        (self.backup / "gitlab/repositories/a.refs").touch()
        self.assertEqual(drp.inspect_backup(self.backup)["storages"], ["default", "secondary"])
        catalog["repositories"][0]["storage"] = "../outside"
        drp.save_json(self.backup / "repositories.json", catalog)
        with self.assertRaises(drp.BadSnapshot):
            drp.inspect_backup(self.backup)


class SyncTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.new, self.old = snapshot("a"), snapshot("b", 27)
        self.home.snapshots = Mock(return_value=[self.new, self.old])
        self.home.download = Mock(return_value={"fixture": True})
        self.home.apply = Mock()
        self.home.container = Mock(return_value={"State": {"Running": True}})
        self.home.remove_container = Mock()

    def sync(self):
        self.home.save_state()
        with contextlib.redirect_stdout(io.StringIO()):
            self.home.sync()

    def test_unchanged_snapshot_neither_downloads_nor_restores(self):
        self.home.state["active"] = self.new["id"]
        self.sync()
        self.home.download.assert_not_called()
        self.home.apply.assert_not_called()

    def test_new_snapshot_is_applied_once(self):
        self.home.state["active"] = self.old["id"]
        self.sync()
        self.home.download.assert_called_once_with(self.new)
        self.home.apply.assert_called_once_with(self.new, {"fixture": True})

    def test_cleanup_resumes_after_success_was_saved_before_power_loss(self):
        self.home.state["active"] = self.new["id"]
        backups = self.home.instance / "data/backups"
        backups.mkdir(parents=True)
        (backups / "leftover").touch()
        live = self.home.instance / "data/live"
        live.write_text("keep")
        self.home.staging.mkdir()
        self.sync()
        self.assertFalse(backups.exists())
        self.assertFalse(self.home.staging.exists())
        self.assertEqual(live.read_text(), "keep")
        self.home.apply.assert_not_called()

    def test_failed_restore_discards_partial_data_then_restores_previous(self):
        self.home.instance.mkdir()
        (self.home.instance / "partial").touch()
        def apply(record, backup):
            if record == self.new:
                self.home.state.update(in_progress=record["id"], active=None)
                raise drp.BadSnapshot("broken backup")
            self.assertFalse(self.home.instance.exists())
        self.home.apply.side_effect = apply
        self.sync()
        self.assertEqual([c.args[0] for c in self.home.download.call_args_list], [self.new, self.old])
        self.assertIn(self.new["id"], drp.load_json(self.home.state_path)["rejected"])
        self.home.remove_container.assert_called_once()

    def test_invalid_download_leaves_existing_good_instance_untouched(self):
        self.home.state["active"] = self.old["id"]
        self.home.download.side_effect = drp.BadSnapshot("bad metadata")
        self.sync()
        self.home.remove_container.assert_not_called()
        self.home.apply.assert_not_called()

    def test_network_credentials_and_disk_errors_do_not_try_older_snapshot(self):
        self.home.state["active"] = self.old["id"]
        self.home.download.side_effect = drp.Failure("network unavailable")
        with self.assertRaises(drp.Failure):
            self.sync()
        self.home.download.assert_called_once()
        self.assertEqual(self.home.state["active"], self.old["id"])
        self.assertEqual(self.home.state["rejected"], {})

    def test_known_bad_latest_does_not_repeatedly_replace_good_instance(self):
        self.home.state.update(active=self.old["id"], rejected={self.new["id"]: "broken"})
        self.sync()
        self.home.download.assert_not_called()

    def test_interrupted_restore_is_cleaned_before_next_download(self):
        self.home.state["in_progress"] = self.new["id"]
        self.home.instance.mkdir()
        (self.home.instance / "partial").touch()
        def download(_):
            self.assertFalse(self.home.instance.exists())
            self.home.remove_container.assert_called_once()
            return {}
        self.home.download.side_effect = download
        self.sync()

    def test_all_bad_snapshots_report_failure(self):
        self.home.download.side_effect = drp.BadSnapshot("broken")
        with self.assertRaises(drp.Failure):
            self.sync()
        self.assertEqual(self.home.download.call_count, 2)

    def test_overlapping_runs_do_not_touch_state(self):
        with drp.locked(self.base / "sync.lock"):
            with self.assertRaises(drp.Failure):
                self.sync()
        self.home.download.assert_not_called()


class SafetyTests(HomeCase):
    def test_restic_is_read_only_and_credentials_are_not_arguments(self):
        self.home.run = Mock(return_value=subprocess.CompletedProcess([], 0, "[]", ""))
        with patch.dict(os.environ, {"AWS_SESSION_TOKEN": "stale", "RESTIC_PASSWORD": "stale"}):
            self.home.restic("snapshots", "--json")
        args, kwargs = self.home.run.call_args
        self.assertEqual(args[0], ["restic", "--no-lock", "snapshots", "--json"])
        self.assertNotIn("fixture-secret", str(args))
        self.assertNotIn("AWS_SESSION_TOKEN", kwargs["env"])
        self.assertNotIn("RESTIC_PASSWORD", kwargs["env"])
        for operation in ("init", "forget", "prune", "unlock", "backup"):
            with self.assertRaises(drp.Failure):
                self.home.restic(operation)

    def test_integrity_failure_allows_fallback_but_network_failure_does_not(self):
        for detail, expected in (("ciphertext verification failed", drp.BadSnapshot),
                                 ("connection refused", drp.Failure),
                                 ("wrong password or no key found", drp.Failure)):
            self.home.run = Mock(return_value=subprocess.CompletedProcess([], 1, "", detail))
            with self.assertRaises(expected) as error:
                self.home.restic("restore", "a" * 64, "--target", "/fixture")
            self.assertIs(type(error.exception), expected)

    def test_snapshot_filters_are_checked_again_and_sorted(self):
        wrong = {**snapshot("c"), "tags": ["gitlab", "test"]}
        self.home.restic = Mock(return_value=json.dumps([snapshot("b", 27), wrong, snapshot("a")]))
        self.assertEqual([item["id"] for item in self.home.snapshots()], ["a" * 64, "b" * 64])

    def test_delete_is_limited_to_owned_work_directories(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.home.staging.symlink_to(outside, target_is_directory=True)
        for path in (outside, self.base, self.home.staging, self.base / "ssh"):
            with self.assertRaises(drp.Failure):
                self.home.remove_tree(path)
        self.assertTrue(outside.exists())

    def test_command_errors_hide_secrets_but_keep_private_diagnostics(self):
        result = subprocess.CompletedProcess([], 1, "sensitive-output", "sensitive-error")
        with patch("drp_home.subprocess.run", return_value=result):
            with self.assertRaises(drp.Failure) as error:
                self.home.run(["fixture"], "Restore")
        self.assertNotIn("sensitive", str(error.exception))
        self.assertEqual(self.home.log_path.stat().st_mode & 0o777, 0o600)

    def test_docker_commands_always_target_this_ubuntu_host(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://production", "DOCKER_CONTEXT": "production"}):
            with patch("drp_home.subprocess.run", return_value=result) as run:
                self.home.run(["docker", "ps"], "Docker")
        self.assertEqual(run.call_args.kwargs["env"]["DOCKER_HOST"], "unix:///var/run/docker.sock")
        self.assertNotIn("DOCKER_CONTEXT", run.call_args.kwargs["env"])

    def test_generated_config_contains_home_urls_and_local_services(self):
        text = drp.gitlab_config("git.example.org", ["default", "secondary"])
        self.assertIn("https://git.example.org:5050", text)
        self.assertIn("omniauth_enabled'] = false", text)
        self.assertIn("sidekiq['enable'] = false", text)
        self.assertIn("/git-data/secondary/repositories", text)
        self.assertIn("registry['registry_http_addr'] = '127.0.0.1:5001'", text)
        self.assertIn("registry_api_url'] = 'http://127.0.0.1:5001'", text)
        self.assertNotIn("sso.example.org", text)
        self.assertNotIn("repository_read_only", text)

    def test_custom_ssh_port_is_published_and_advertised_consistently(self):
        self.home.config["ssh_port"] = 23022
        self.home.run = Mock()
        self.home.start_container("fixture-image", published=True)
        args = self.home.run.call_args.args[0]
        self.assertIn("23022:2222", args)
        self.assertNotIn("2222:2222", args)
        text = drp.gitlab_config("git.example.org", ["default"], ssh_port=self.home.config["ssh_port"])
        self.assertIn("gitlab_shell_ssh_port'] = 23022", text)
        self.assertIn("gitlab_sshd['listen_address'] = '0.0.0.0:2222'", text)
        self.home.start_container("fixture-image", published=False)
        self.assertNotIn("-p", self.home.run.call_args.args[0])

    def test_legacy_config_keeps_original_ssh_port(self):
        self.assertEqual(self.home.config["ssh_port"], 2222)
        self.home.run = Mock()
        self.home.start_container("fixture-image", published=True)
        self.assertIn("2222:2222", self.home.run.call_args.args[0])

    def test_saved_custom_ssh_port_is_loaded_on_next_run(self):
        config = drp.load_json(self.etc / "config.json")
        config["ssh_port"] = 23022
        drp.save_json(self.etc / "config.json", config)
        self.assertEqual(drp.Home(self.base, self.etc).config["ssh_port"], 23022)

    def test_proxy_nginx_preserves_registry_host_and_https_scheme(self):
        text = drp.nginx_config("git.example.org", "192.168.1.7", cert_name="git.example.org")
        self.assertIn("listen 5050 ssl", text)
        self.assertIn("proxy_pass http://192.168.1.7:5080", text)
        self.assertIn("proxy_set_header Host $http_host", text)
        self.assertIn("proxy_set_header X-Forwarded-Proto https", text)
        self.assertNotIn("$http_connection", text)
        self.assertEqual(text.count("map $http_upgrade $drp_connection_upgrade"), 1)
        self.assertLess(text.index("map $http_upgrade"), text.index("server {"))
        self.assertEqual(text.count("proxy_set_header Connection $drp_connection_upgrade;"), 2)
        self.assertNotIn("ssl_certificate", drp.nginx_config("git.example.org", tls=False))

    def test_domain_validation_rejects_shell_and_nginx_injection(self):
        for domain in ("https://git.example.org", "x;include /etc/passwd", "$(id).example.org",
                       "example.org:443", "*.example.org", "a\nb.example.org"):
            with self.assertRaises(drp.Failure):
                drp.domain_name(domain)

    def test_proxy_instructions_refresh_examples_with_saved_network_settings(self):
        self.home.config.update(mode="proxy", domain="git.22222.example.org",
                                bind_ip="192.168.1.15", ssh_port=23022)
        (self.etc / "nginx.conf.example").write_text("stale template")
        with patch.object(drp, "ETC", self.etc), contextlib.redirect_stdout(io.StringIO()):
            drp.access_instructions(self.home.config)
        guide = (self.etc / "nginx-setup.txt").read_text()
        self.assertIn("https://git.22222.example.org:5050/v2/", guide)
        self.assertIn("http://192.168.1.15:5080/v2/", guide)
        self.assertIn("-p 23022 git.22222.example.org", guide)
        self.assertNotIn("192.168.1.10", guide)
        self.assertIn(str(self.etc / "nginx.conf.example"), guide)
        tls = (self.etc / "nginx.conf.example").read_text()
        self.assertIn("map $http_upgrade $drp_connection_upgrade", tls)
        self.assertIn("server_name git.22222.example.org;", tls)
        self.assertIn("proxy_pass http://192.168.1.15:5080;", tls)
        self.assertNotIn("ssl_certificate", (self.etc / "nginx-http.conf.example").read_text())

    def test_proxy_instructions_load_installed_document(self):
        installed = self.root / "installed"
        installed.mkdir()
        shutil.copyfile(drp.LIB.parent / "docs/nginx.md", installed / "nginx.md")
        with patch.object(drp, "LIB", installed):
            text = drp.proxy_guide("home.example.org", "192.168.2.10", 24022)
        self.assertIn("https://home.example.org:5050/v2/", text)
        self.assertIn("ssh-keyscan -T 5 -p 24022 home.example.org", text)

    def test_failed_restore_is_never_published(self):
        fixture(self.home.staging)
        backup = drp.inspect_backup(self.home.staging)
        self.home.container = Mock(return_value=None)
        self.home.remove_container = Mock()
        self.home.start_container = Mock()
        self.home.wait_ready = Mock()
        def run(args, label, **kwargs):
            if "gitlab-backup" in args:
                raise drp.BadSnapshot("restore failed")
            return subprocess.CompletedProcess(args, 0, "", "")
        self.home.run = Mock(side_effect=run)
        with self.assertRaises(drp.BadSnapshot):
            self.home.apply(snapshot("a"), backup)
        self.home.start_container.assert_called_once_with(backup["image"], published=False)
        self.assertEqual(drp.load_json(self.home.state_path)["in_progress"], "a" * 64)

    def prepare_apply(self, doctor="Total: 0 row(s) affected"):
        fixture(self.home.staging)
        backup = drp.inspect_backup(self.home.staging)
        self.home.container = Mock(return_value=None)
        self.home.remove_container = Mock()
        self.home.wait_ready = Mock()
        self.events = []
        self.home.start_container = Mock(side_effect=lambda image, published: self.events.append(("publish", published)))
        def run(args, label, **kwargs):
            self.events.append(("run", list(args)))
            if "gitlab-ctl" in args:
                # Match the public CLI, not the larger internal list of sv methods.
                self.assertIn(args[args.index("gitlab-ctl") + 1],
                              {"reconfigure", "start", "stop", "restart", "status"})
            output = ""
            if "/drp/configure_gitlab.rb" in args:
                output = "DRP_CONFIGURED users=1 projects=0\n"
            if "/drp/verify_repositories.rb" in args:
                output = "DRP_REPOSITORIES_VERIFIED=0\n"
            if "gitlab:doctor:secrets" in args:
                output = doctor
            if "http://127.0.0.1:5000/v2/" in args:
                output = ('HTTP/1.1 401 Unauthorized\nDocker-Distribution-Api-Version: registry/2.0\n'
                          'WWW-Authenticate: Bearer realm="https://git.example.org/jwt/auth"\n')
            return subprocess.CompletedProcess(args, 0, output, "")
        self.home.run = Mock(side_effect=run)
        return backup

    def test_success_publishes_after_checks_and_preserves_ssh_keys(self):
        self.home.config["ssh_port"] = 23022
        backup = self.prepare_apply()
        (self.base / "ssh").mkdir()
        key = self.base / "ssh/ssh_host_ed25519_key"
        key.write_text("fixture-ssh-key")
        with contextlib.redirect_stdout(io.StringIO()):
            self.home.apply(snapshot("a"), backup)
        state = drp.load_json(self.home.state_path)
        self.assertEqual(state["active"], "a" * 64)
        self.assertIsNone(state["in_progress"])
        self.assertEqual(key.read_text(), "fixture-ssh-key")
        self.assertIn("gitlab_shell_ssh_port'] = 23022", (self.home.instance / "config/gitlab.rb").read_text())
        self.assertFalse(self.home.staging.exists())
        self.assertFalse((self.home.instance / "data/backups").exists())
        published = self.events.index(("publish", True))
        checks = [i for i, (kind, args) in enumerate(self.events) if kind == "run" and
                  ("/drp/verify_repositories.rb" in args or "gitlab:doctor:secrets" in args)]
        self.assertTrue(checks and all(i < published for i in checks))
        self.assertFalse(any(kind == "run" and args[:2] == ["docker", "pull"] for kind, args in self.events))

    def test_puma_start_is_followed_by_readiness_before_publication(self):
        backup = self.prepare_apply()
        original_run = self.home.run.side_effect
        started = False
        ready_checks = 0
        def run(args, label, **kwargs):
            nonlocal started
            if "/opt/gitlab/embedded/bin/sv" in args and "up" in args:
                self.assertEqual(args[-1], "/opt/gitlab/service/puma")
                started = True
            return original_run(args, label, **kwargs)
        def ready():
            nonlocal ready_checks
            ready_checks += 1
            if ready_checks > 1:
                self.assertTrue(started)
            if ready_checks == 2:
                self.assertNotIn(("publish", True), self.events)
        self.home.run.side_effect = run
        self.home.wait_ready.side_effect = ready
        with contextlib.redirect_stdout(io.StringIO()):
            self.home.apply(snapshot("a"), backup)
        self.assertTrue(started)
        self.assertEqual(ready_checks, 3)
        self.assertEqual(drp.load_json(self.home.state_path)["active"], "a" * 64)

    def test_puma_start_error_prevents_readiness_and_publication(self):
        backup = self.prepare_apply()
        original_run = self.home.run.side_effect
        def run(args, label, **kwargs):
            if "/opt/gitlab/embedded/bin/sv" in args and "up" in args:
                raise drp.Failure("supervisor unavailable")
            return original_run(args, label, **kwargs)
        self.home.run.side_effect = run
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(drp.Failure):
            self.home.apply(snapshot("a"), backup)
        self.home.wait_ready.assert_called_once()  # Only the fresh instance was checked.
        self.assertNotIn(("publish", True), self.events)
        self.assertIsNone(drp.load_json(self.home.state_path)["active"])

    def test_puma_stop_failure_never_starts_restore_or_publishes(self):
        for output in ("timeout: run: puma: (pid 2796) 123s, want down\n",
                       "fail: /opt/gitlab/service/puma: unable to open supervise/ok\n"):
            with self.subTest(output=output):
                backup = self.prepare_apply()
                original_run = self.home.run.side_effect
                def run(args, label, **kwargs):
                    if "stop" in args and "/opt/gitlab/service/puma" in args:
                        return subprocess.CompletedProcess(args, 1, output, "")
                    return original_run(args, label, **kwargs)
                self.home.run.side_effect = run
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(drp.Failure) as error:
                    self.home.apply(snapshot("a"), backup)
                self.assertNotIsInstance(error.exception, drp.BadSnapshot)
                self.assertIn("восстановление БД не запущено", str(error.exception))
                self.assertFalse(any("gitlab-backup" in call.args[0]
                                     for call in self.home.run.call_args_list))
                self.home.start_container.assert_called_once_with(backup["image"], published=False)
                self.assertTrue((self.home.staging / "gitlab/db/database.sql.gz").is_file())
                self.assertIsNone(drp.load_json(self.home.state_path)["active"])

    def test_slow_puma_stop_is_allowed_before_restore(self):
        backup = self.prepare_apply()
        original_run = self.home.run.side_effect
        stopped = False
        def run(args, label, **kwargs):
            nonlocal stopped
            if "stop" in args and "/opt/gitlab/service/puma" in args:
                # Model a service needing 20 seconds, longer than runit's default.
                wait = int(args[args.index("-w") + 1]) if "-w" in args else 7
                self.assertGreater(kwargs["timeout"], wait)
                stopped = wait >= 20
                return subprocess.CompletedProcess(args, 0 if stopped else 1,
                                                   "ok: down: puma\n" if stopped else "timeout\n", "")
            if "gitlab-backup" in args:
                self.assertTrue(stopped, "Database restore must wait for Puma to stop")
            return original_run(args, label, **kwargs)
        self.home.run.side_effect = run
        with contextlib.redirect_stdout(io.StringIO()):
            self.home.apply(snapshot("a"), backup)
        self.assertTrue(stopped)
        self.assertEqual(drp.load_json(self.home.state_path)["active"], "a" * 64)

    def test_doctor_nonzero_failures_with_zero_exit_code_prevent_publication(self):
        backup = self.prepare_apply(doctor="Total: 3 row(s) affected")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(drp.BadSnapshot):
            self.home.apply(snapshot("a"), backup)
        self.assertNotIn(("publish", True), self.events)
        self.assertIsNone(drp.load_json(self.home.state_path)["active"])

    def test_registry_with_production_auth_url_is_rejected(self):
        self.home.run = Mock(return_value=subprocess.CompletedProcess([], 0,
            'HTTP/1.1 401 Unauthorized\nDocker-Distribution-Api-Version: registry/2.0\n'
            'WWW-Authenticate: Bearer realm="https://primary.example.org/jwt/auth"\n', ""))
        with self.assertRaises(drp.BadSnapshot):
            self.home.verify_registry()


@unittest.skipUnless(shutil.which("restic"), "restic is not installed")
class ResticIntegrationTests(HomeCase):
    def test_real_snapshot_download_and_verify_preserve_future_components(self):
        repo = self.root / "repository"
        source = self.root / ".restic-staging"
        fixture(source)
        (source / "gitlab/future_component.tar.gz").write_bytes(b"future component")
        env = {"PATH": os.environ["PATH"], "RESTIC_REPOSITORY": str(repo), "RESTIC_PASSWORD": "fixture",
               "RESTIC_CACHE_DIR": str(self.root / "cache")}
        for args in (["init"], ["backup", str(source), "--host", PROFILE["source_host"],
                                "--tag", "gitlab,production"]):
            result = subprocess.run(["restic", *args], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.home.config["repository"] = str(repo)
        (self.etc / "restic-password").write_text("fixture\n")
        selected = self.home.snapshots()
        self.assertEqual(len(selected), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            info = self.home.download(selected[0])
        self.assertEqual(info["image"], "docker.io/gitlab/gitlab-ce:19.4.3-ce.0")
        self.assertEqual((self.home.staging / "gitlab/future_component.tar.gz").read_bytes(), b"future component")
        self.assertFalse(list((repo / "locks").glob("*")))


if __name__ == "__main__":
    unittest.main()
