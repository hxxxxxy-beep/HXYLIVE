import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENABLE_BBR = ROOT / "deploy" / "enable-bbr.sh"
INSTALL_VPS = ROOT / "deploy" / "install-vps.sh"
VERIFY_VPS = ROOT / "deploy" / "verify-vps.sh"
BOOTSTRAP = ROOT / "deploy" / "bootstrap-host.sh"
AI_REDEPLOY = ROOT / "docs" / "AI_REDEPLOY.md"
AGENTS = ROOT / "AGENTS.md"


class EnableBbrDeployTests(unittest.TestCase):
    def test_shell_scripts_have_valid_syntax(self):
        for path in (ENABLE_BBR, INSTALL_VPS, VERIFY_VPS, BOOTSTRAP):
            with self.subTest(path=path.name):
                subprocess.run(["sh", "-n", str(path)], check=True)

    def test_install_calls_enable_bbr(self):
        text = INSTALL_VPS.read_text()
        self.assertIn("deploy/enable-bbr.sh", text)

    def test_verify_asserts_bbr_when_supported(self):
        text = VERIFY_VPS.read_text()
        self.assertIn("tcp_congestion_control", text)
        self.assertIn("bbr", text)
        self.assertIn("99-bbr.conf", text)

    def test_enable_bbr_persists_sysctl_and_module(self):
        text = ENABLE_BBR.read_text()
        self.assertIn("net.core.default_qdisc=fq", text)
        self.assertIn("net.ipv4.tcp_congestion_control=bbr", text)
        self.assertIn("/etc/modules-load.d/bbr.conf", text)
        self.assertIn("/etc/sysctl.d/99-bbr.conf", text)

    def test_docs_mention_bbr(self):
        self.assertIn("enable-bbr.sh", AI_REDEPLOY.read_text())
        self.assertIn("enable-bbr.sh", AGENTS.read_text())


if __name__ == "__main__":
    unittest.main()
