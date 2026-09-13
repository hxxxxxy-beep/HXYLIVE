import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL_VPS = ROOT / "deploy" / "install-vps.sh"
VERIFY_VPS = ROOT / "deploy" / "verify-vps.sh"
BOOTSTRAP = ROOT / "deploy" / "bootstrap-host.sh"
INSTALL_REALITY = ROOT / "deploy" / "install-reality.sh"
SET_MODE = ROOT / "deploy" / "set-access-mode.sh"
RECOMMEND = ROOT / "deploy" / "recommend-reality-dest.sh"
ENV_EXAMPLE = ROOT / ".env.example"
GITIGNORE = ROOT / ".gitignore"
AI_REDEPLOY = ROOT / "docs" / "AI_REDEPLOY.md"
AGENTS = ROOT / "AGENTS.md"


APPLY_MIXIN = ROOT / "mac-helper" / "apply-clash-reality-mixin.sh"


class RealityDeployTests(unittest.TestCase):
    def test_shell_scripts_have_valid_syntax(self):
        for path in (
            INSTALL_REALITY,
            SET_MODE,
            RECOMMEND,
            INSTALL_VPS,
            VERIFY_VPS,
            BOOTSTRAP,
            APPLY_MIXIN,
        ):
            with self.subTest(path=path.name):
                subprocess.run(["sh", "-n", str(path)], check=True)

    def test_install_reality_emits_merge_mixin(self):
        text = INSTALL_REALITY.read_text()
        self.assertIn("clash-meta-mixin.yaml", text)
        self.assertIn("prepend-proxies", text)
        self.assertIn("prepend-rules", text)
        self.assertIn("hxylive-reality", text)
        self.assertIn("MAC_CLASH.txt", text)

    def test_apply_mixin_script_is_durable(self):
        text = APPLY_MIXIN.read_text()
        self.assertIn("clash-reality-mixin.yaml", text)
        self.assertIn("HXYLIVE Reality", text)
        self.assertIn("Merge_hxylive", text)
        self.assertIn("prepend-proxies", text)

    def test_install_vps_wires_reality_by_default(self):
        text = INSTALL_VPS.read_text()
        self.assertIn("deploy/install-reality.sh", text)
        self.assertIn("deploy/set-access-mode.sh", text)
        self.assertIn("ENABLE_REALITY", text)

    def test_set_access_mode_toggles_8080_and_xray(self):
        text = SET_MODE.read_text()
        self.assertIn("deny 8080/tcp", text)
        self.assertIn("allow 8080/tcp", text)
        self.assertIn("systemctl restart xray", text)
        self.assertIn("systemctl stop xray", text)
        self.assertIn("reality", text)
        self.assertIn("direct", text)

    def test_verify_checks_access_mode(self):
        text = VERIFY_VPS.read_text()
        self.assertIn("access-mode", text)
        self.assertIn("xray", text)
        self.assertIn("443", text)
        self.assertIn("clash-meta-mixin.yaml", text)

    def test_env_example_defaults_reality_on(self):
        text = ENV_EXAMPLE.read_text()
        self.assertIn("ENABLE_REALITY=1", text)
        self.assertIn("REALITY_DEST=", text)

    def test_gitignore_excludes_reality_secrets(self):
        self.assertIn("/reality/", GITIGNORE.read_text())

    def test_docs_cover_reality_handoff(self):
        ai = AI_REDEPLOY.read_text()
        agents = AGENTS.read_text()
        for text in (ai, agents):
            self.assertIn("Reality", text)
            self.assertIn("ENABLE_REALITY", text)
            self.assertIn("set-access-mode.sh", text)
            self.assertIn("apply-clash-reality-mixin.sh", text)
        self.assertIn("Cloudflare Tunnel", ai)
        self.assertIn("REALITY_DEST", ai)
        self.assertIn("clash-meta-mixin.yaml", ai)


if __name__ == "__main__":
    unittest.main()
