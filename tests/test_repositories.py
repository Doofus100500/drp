"""Exercise the Ruby verifier with actual Git objects and refs, without Docker."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("ruby") and shutil.which("git"), "ruby and git are required")
class RepositoryVerificationTests(unittest.TestCase):
    def test_matching_refs_pass_and_missing_or_corrupt_objects_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repositories = root / "git-data/default/repositories"
            repositories.mkdir(parents=True)
            repo = repositories / "project.git"
            subprocess.run(["git", "init", "--bare", str(repo)], capture_output=True, check=True)
            def git(*args, data=None):
                return subprocess.run(["git", "--git-dir=" + str(repo), *args], input=data,
                                      text=True, capture_output=True, check=True).stdout.strip()
            blob = git("hash-object", "-w", "--stdin", data="fixture\n")
            tree = git("mktree", data=f"100644 blob {blob}\tfile.txt\n")
            commit = git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.org",
                         "commit-tree", tree, "-m", "fixture")
            git("update-ref", "refs/heads/main", commit)
            refs = root / "backups"
            refs.mkdir()
            reference = refs / "project.refs"
            reference.write_text(f"{commit} refs/heads/main\n")
            # Replace only container filesystem locations in the test harness.
            original = Path(__file__).resolve().parents[1] / "scripts/verify_repositories.rb"
            source = original.read_text().replace("/opt/gitlab/embedded/bin/git", shutil.which("git"))
            source = source.replace("/var/opt/gitlab/git-data", str(root / "git-data"))
            source = source.replace("/var/opt/gitlab/backups/repositories", str(refs))
            verifier = root / "verify.rb"
            verifier.write_text(source)
            payload = json.dumps({"repositories": [{
                "storage": "default", "relative_path": "project.git", "state": "present",
                "steps": [{"ref_path": "project.refs"}]}]})
            def verify():
                return subprocess.run(["ruby", str(verifier)], input=payload,
                                      capture_output=True, text=True)
            result = verify()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DRP_REPOSITORIES_VERIFIED=1", result.stdout)
            reference.write_text(f"{commit} refs/heads/missing\n")
            self.assertNotEqual(verify().returncode, 0)
            reference.write_text(f"{commit} refs/heads/main\n")
            (repo / "objects" / blob[:2] / blob[2:]).unlink()
            self.assertNotEqual(verify().returncode, 0)


if __name__ == "__main__":
    unittest.main()
