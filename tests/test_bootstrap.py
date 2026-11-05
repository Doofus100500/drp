import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "TEST_ROOT": str(self.root), "MODE": "first-fails", "INSTALL_EXIT": "0"}
        files = ["install.sh", "docs/nginx.md", "scripts/drp_home.py", "scripts/configure_gitlab.rb",
                 "scripts/verify_repositories.rb", "templates/infra-git-drp-home-sync.service",
                 "templates/infra-git-drp-home-sync.timer", "templates/infra-git-drp-home-cert.service",
                 "templates/infra-git-drp-home-cert.timer"]
        with tarfile.open(self.root / "good.tar.gz", "w:gz") as archive:
            for name in files:
                text = ('#!/usr/bin/env bash\nprintf "run\\n" >> "$TEST_ROOT/installed"\n'
                        'exit "$INSTALL_EXIT"\n') if name == "install.sh" else "fixture\n"
                info = tarfile.TarInfo("drp-master/" + name)
                info.size = len(text.encode())
                archive.addfile(info, io.BytesIO(text.encode()))
        (self.root / "fake-bootstrap.sh").write_text(
            '#!/usr/bin/env bash\nprintf "bootstrap\\n" >> "$TEST_ROOT/installed"\nexit "$INSTALL_EXIT"\n')
        curl = self.bin / "curl"
        curl.write_text("#!" + sys.executable + "\n" + '''
import json, os, pathlib, shutil, sys
root = pathlib.Path(os.environ["TEST_ROOT"])
args = sys.argv[1:]
url = next(arg for arg in args if arg.startswith("https://"))
output = pathlib.Path(args[args.index("--output" if "--output" in args else "-o") + 1])
with (root / "requests").open("a") as log:
    log.write(json.dumps(args) + "\\n")
mode = os.environ["MODE"]
first = "github.com/" in url or "raw.githubusercontent.com/" in url
if mode == "both-fail" or (mode == "first-fails" and first):
    output.write_text("partial download")
    sys.exit(28)
if mode == "invalid-first" and first:
    output.write_text("this is not an archive")
elif mode == "empty-first" and first:
    output.write_text("")
else:
    shutil.copyfile(root / ("fake-bootstrap.sh" if url.endswith(".sh") else "good.tar.gz"), output)
''')
        curl.chmod(0o755)
        for name, text in (("apt-get", "exit 0"), ("sudo", 'exec "$@"')):
            path = self.bin / name
            path.write_text("#!/usr/bin/env bash\n" + text + "\n")
            path.chmod(0o755)

    def run_download(self, mode, exit_code=0):
        self.env.update(MODE=mode, INSTALL_EXIT=str(exit_code))
        script = ('source ' + shlex.quote(str(ROOT / "bootstrap.sh")) + '\n'
                  'download_project "$TEST_ROOT/work"\n'
                  'bash "$TEST_ROOT/work/source/install.sh"\n')
        return subprocess.run(["bash", "-c", script], env=self.env, text=True, capture_output=True)

    def requests(self):
        return [json.loads(line) for line in (self.root / "requests").read_text().splitlines()]

    def test_github_unavailable_downloads_gitlab_and_installs_once(self):
        result = self.run_download("first-fails")
        self.assertEqual(result.returncode, 0, result.stderr)
        requests = self.requests()
        self.assertEqual(len(requests), 2)
        self.assertIn("github.com", requests[0][-1])
        self.assertIn("gitlab.com", requests[1][-1])
        for args in requests:
            self.assertIn("--connect-timeout", args)
            self.assertIn("--max-time", args)
        self.assertEqual((self.root / "installed").read_text(), "run\n")

    def test_corrupt_archive_uses_second_source(self):
        result = self.run_download("invalid-first")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.requests()), 2)

    def test_both_downloads_fail_without_executing_installation(self):
        result = self.run_download("both-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.requests()), 2)
        self.assertFalse((self.root / "installed").exists())

    def test_installer_error_does_not_download_or_execute_other_source(self):
        result = self.run_download("ok", exit_code=17)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual((self.root / "installed").read_text(), "run\n")

    def run_oneliner(self, mode, exit_code=0):
        self.env.update(MODE=mode, INSTALL_EXIT=str(exit_code))
        command = (ROOT / "README.md").read_text().split("```bash\n", 1)[1].split("\n```", 1)[0]
        return subprocess.run(["bash", "-c", command], env=self.env, text=True, capture_output=True)

    def test_documented_oneliner_fetches_bootstrap_from_gitlab_when_github_fails(self):
        result = self.run_oneliner("first-fails")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.requests()), 2)
        self.assertEqual((self.root / "installed").read_text(), "bootstrap\n")

    def test_documented_oneliner_does_not_accept_empty_bootstrap(self):
        result = self.run_oneliner("empty-first")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.requests()), 2)
        self.assertEqual((self.root / "installed").read_text(), "bootstrap\n")

    def test_documented_oneliner_preserves_installer_error_without_retry(self):
        result = self.run_oneliner("ok", exit_code=17)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(len(self.requests()), 1)

    def test_documented_oneliner_fails_when_neither_mirror_is_available(self):
        result = self.run_oneliner("both-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "installed").exists())

    def run_main(self, mode, exit_code=0):
        # Substitute only the host prerequisites; exercise the real main/trap on macOS too.
        script = (ROOT / "bootstrap.sh").read_text().replace('[[ $EUID -ne 0 ]]', 'false')
        script = script.replace('source /etc/os-release', 'source "$TEST_ROOT/os-release"')
        (self.root / "os-release").write_text("ID=ubuntu\nVERSION_ID=26.04\n")
        (self.root / "bootstrap.sh").write_text(script)
        (self.bin / "uname").write_text("#!/bin/sh\necho x86_64\n")
        (self.bin / "uname").chmod(0o755)
        tmp = self.root / "tmp"
        tmp.mkdir()
        self.env.update(MODE=mode, INSTALL_EXIT=str(exit_code), TMPDIR=str(tmp))
        result = subprocess.run(["bash", str(self.root / "bootstrap.sh")], env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(list(tmp.iterdir()), [], result.stderr)
        return result

    def test_main_preserves_installer_exit_and_cleans_temporary_directory(self):
        result = self.run_main("ok", exit_code=17)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(len(self.requests()), 1)

    def test_main_cleans_partial_download_after_both_mirrors_fail(self):
        result = self.run_main("both-fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "installed").exists())


if __name__ == "__main__":
    unittest.main()
