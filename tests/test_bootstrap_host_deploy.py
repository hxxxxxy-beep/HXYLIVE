import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "deploy" / "bootstrap-host.sh"
INSTALL_VPS = ROOT / "deploy" / "install-vps.sh"
VERIFY_VPS = ROOT / "deploy" / "verify-vps.sh"
ENABLE_BBR = ROOT / "deploy" / "enable-bbr.sh"
AI_REDEPLOY = ROOT / "docs" / "AI_REDEPLOY.md"
AGENTS = ROOT / "AGENTS.md"
README = ROOT / "README.md"
HANDOFF_RULE = ROOT / ".cursor" / "rules" / "url-only-handoff.mdc"


class BootstrapHostDeployTests(unittest.TestCase):
    def test_shell_scripts_have_valid_syntax(self):
        for path in (BOOTSTRAP, INSTALL_VPS, VERIFY_VPS, ENABLE_BBR):
            with self.subTest(path=path.name):
                subprocess.run(["sh", "-n", str(path)], check=True)

    def test_bootstrap_targets_debian_family_and_docker_official_repo(self):
        text = BOOTSTRAP.read_text()
        self.assertIn("debian|ubuntu", text)
        self.assertIn("download.docker.com/linux", text)
        self.assertIn("docker-compose-plugin", text)
        self.assertIn("nginx", text)

    def test_install_calls_bootstrap_before_compose(self):
        text = INSTALL_VPS.read_text()
        self.assertIn("deploy/bootstrap-host.sh", text)
        self.assertLess(
            text.index("deploy/bootstrap-host.sh"),
            text.index("docker compose"),
        )

    def test_docs_mention_debian_and_bootstrap(self):
        for path in (AI_REDEPLOY, AGENTS, README, HANDOFF_RULE):
            with self.subTest(path=path.name):
                text = path.read_text()
                self.assertIn("bootstrap-host.sh", text)
                self.assertRegex(text, r"(?i)debian")

    def test_ai_redeploy_forbids_manual_docker_nginx(self):
        text = AI_REDEPLOY.read_text()
        self.assertIn("Do not ask me to manually install Docker/Nginx", text)
        self.assertIn("Debian 12+", text)


if __name__ == "__main__":
    unittest.main()
