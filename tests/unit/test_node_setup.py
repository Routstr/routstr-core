"""Offline contracts for the first-run node workflow."""

import importlib.util
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "node_setup.py"
spec = importlib.util.spec_from_file_location("node_setup", SCRIPT)
assert spec and spec.loader
node_setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node_setup)


class NodeSetupTests(unittest.TestCase):
    def test_env_created_once_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env.example").write_text("# example\n")
            self.assertTrue(node_setup.prepare_env(root))
            self.assertEqual((root / ".env").stat().st_mode & 0o777, 0o600)
            (root / ".env").write_text("KEEP=1\n")
            self.assertFalse(node_setup.prepare_env(root))
            self.assertEqual((root / ".env").read_text(), "KEEP=1\n")
            (root / ".env").write_text("NSEC=private\n")
            with self.assertRaises(RuntimeError):
                node_setup.prepare_env(root)
            (root / ".env").unlink()
            (root / ".env").symlink_to(root / ".env.example")
            with self.assertRaises(RuntimeError):
                node_setup.prepare_env(root)

    def test_prepare_env_applies_overrides_on_create(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env.example").write_text("# example\nHTTP_URL=\n")
            created = node_setup.prepare_env(
                root, {"HTTP_URL": "https://node.example", "RECEIVE_LN_ADDRESS": "me@host"}
            )
            self.assertTrue(created)
            text = (root / ".env").read_text()
            self.assertIn("HTTP_URL=https://node.example", text)
            self.assertIn("RECEIVE_LN_ADDRESS=me@host", text)

    def test_prepare_env_upserts_without_clobbering(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text("KEEP=1\nHTTP_URL=\n")
            node_setup.prepare_env(root, {"HTTP_URL": "https://node.example"})
            text = (root / ".env").read_text()
            self.assertIn("KEEP=1", text)
            self.assertIn("HTTP_URL=https://node.example", text)

    def test_prepare_env_rejects_conflicting_public_settings(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env").write_text("HTTP_URL=https://a.example\n")
            with self.assertRaises(RuntimeError):
                node_setup.prepare_env(root, {"HTTP_URL": "https://b.example"})
            (root / ".env").write_text("ONION_URL=http://abc.onion\n")
            with self.assertRaises(RuntimeError):
                node_setup.prepare_env(root)

    def test_public_origin_only(self) -> None:
        self.assertEqual(node_setup.public_url("https://node.example/"), "https://node.example")
        for value in ("http://node.example", "https://name:pass@node.example", "https://node.example/admin", "https://node.example/?token=x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                node_setup.public_url(value)

    def test_payout_address_validation(self) -> None:
        with patch.object(node_setup, "_fetch_json", return_value={"tag": "payRequest"}):
            self.assertEqual(
                node_setup.validate_payout_address("me@wallet.example"),
                ("me@wallet.example", True),
            )
        with patch.object(node_setup, "_fetch_json", return_value={"tag": "payRequest"}):
            self.assertEqual(
                node_setup.validate_payout_address("lightning:me@wallet.example"),
                ("lightning:me@wallet.example", True),
            )
        with patch.object(node_setup, "_fetch_json", return_value={"tag": "other"}):
            with self.assertRaises(ValueError):
                node_setup.validate_payout_address("me@wallet.example")
        with patch.object(node_setup, "_fetch_json", side_effect=urllib.error.URLError("down")):
            self.assertEqual(
                node_setup.validate_payout_address("me@wallet.example"),
                ("me@wallet.example", False),
            )
        self.assertEqual(
            node_setup.validate_payout_address("lnurl1dp68gurn8ghj7"), ("lnurl1dp68gurn8ghj7", False)
        )
        for value in ("not-an-address", "user@", "@host", "ftp://host/lnurl"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                node_setup.validate_payout_address(value)

    def test_probe_distinguishes_reachable_from_configured(self) -> None:
        with patch.object(node_setup, "get_json", side_effect=[{"name": "Node"}, {"data": []}]):
            self.assertEqual(node_setup.probe("http://127.0.0.1:8000"), ("Node", 0))
        with patch.object(node_setup, "get_json", side_effect=[{"name": "Node"}, {"data": [{"id": "model"}]}]):
            self.assertEqual(node_setup.probe("http://127.0.0.1:8000"), ("Node", 1))
        with patch.object(node_setup, "get_json", side_effect=[{"name": "Node"}, {}]):
            with self.assertRaises(RuntimeError):
                node_setup.probe("http://127.0.0.1:8000")

    def test_check_requires_models(self) -> None:
        with patch.object(sys, "argv", ["node_setup.py", "check", "--port", "18080"]):
            with patch.object(node_setup, "probe", return_value=("Node", 0)) as probe:
                self.assertEqual(node_setup.main(), 2)
                probe.assert_called_once_with("http://127.0.0.1:18080")
            with patch.object(node_setup, "probe", return_value=("Node", 1)):
                self.assertEqual(node_setup.main(), 0)

    def test_start_requires_a_mode(self) -> None:
        with patch.object(sys, "argv", ["node_setup.py", "start"]):
            with self.assertRaises(SystemExit):
                node_setup.main()

    def test_start_rejects_both_modes(self) -> None:
        argv = ["node_setup.py", "start", "--private", "--public-url", "https://node.example"]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit):
                node_setup.main()

    def test_check_rejects_start_only_flags(self) -> None:
        with patch.object(sys, "argv", ["node_setup.py", "check", "--ln-address", "me@host"]):
            with self.assertRaises(SystemExit):
                node_setup.main()

    def test_private_start_passes_selected_port_to_compose(self) -> None:
        with patch.object(sys, "argv", ["node_setup.py", "start", "--private", "--no-cli-token", "--port", "18080"]):
            with patch.object(node_setup.shutil, "which", return_value="/usr/bin/docker"):
                with patch.object(node_setup, "prepare_env", return_value=False) as prepare:
                    with patch.object(node_setup.subprocess, "run") as run:
                        with patch.object(node_setup, "probe", return_value=("Node", 0)) as probe:
                            self.assertEqual(node_setup.main(), 0)
                            probe.assert_called_once_with("http://127.0.0.1:18080")
                            self.assertEqual(prepare.call_args.args[1], {})
                            self.assertEqual(run.call_count, 2)
                            self.assertTrue(all(call.kwargs["env"]["ROUTSTR_NODE_PORT"] == "18080" for call in run.call_args_list))

    def test_public_start_sets_http_url_and_preflights(self) -> None:
        argv = [
            "node_setup.py",
            "start",
            "--public-url",
            "https://node.example/",
            "--no-cli-token",
            "--ln-address",
            "me@wallet.example",
            "--min-payout-sat",
            "500",
        ]
        with patch.object(sys, "argv", argv):
            with patch.object(node_setup.shutil, "which", return_value="/usr/bin/docker"):
                with patch.object(node_setup, "preflight_origin") as preflight:
                    with patch.object(node_setup, "validate_payout_address", return_value=("me@wallet.example", True)):
                        with patch.object(node_setup, "prepare_env", return_value=False) as prepare:
                            with patch.object(node_setup.subprocess, "run"):
                                with patch.object(node_setup, "probe", return_value=("Node", 1)) as probe:
                                    self.assertEqual(node_setup.main(), 0)
                                    preflight.assert_called_once_with("https://node.example")
                                    overrides = prepare.call_args.args[1]
                                    self.assertEqual(overrides["HTTP_URL"], "https://node.example")
                                    self.assertEqual(overrides["RECEIVE_LN_ADDRESS"], "me@wallet.example")
                                    self.assertEqual(overrides["MIN_PAYOUT_SAT"], "500")
                                    self.assertEqual(probe.call_args_list[0].args[0], "http://127.0.0.1:8000")

    def test_public_start_does_not_block_on_unreachable_origin(self) -> None:
        with patch.object(sys, "argv", ["node_setup.py", "start", "--public-url", "https://node.example", "--no-cli-token"]):
            with patch.object(node_setup.shutil, "which", return_value="/usr/bin/docker"):
                with patch.object(node_setup, "preflight_origin"):
                    with patch.object(node_setup, "prepare_env", return_value=False):
                        with patch.object(node_setup.subprocess, "run"):
                            with patch.object(node_setup, "probe", return_value=("Node", 1)):
                                self.assertEqual(node_setup.main(), 0)

    def test_preflight_rejects_dead_origin(self) -> None:
        with patch.object(node_setup.urllib.request, "urlopen", side_effect=urllib.error.URLError("no dns")):
            with self.assertRaises(RuntimeError):
                node_setup.preflight_origin("https://node.example")

    def test_public_start_writes_cli_config(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / ".routstr" / "config.json"
            argv = [
                "node_setup.py",
                "start",
                "--public-url",
                "https://node.example",
                "--cli-config",
                str(config),
            ]
            with patch.object(sys, "argv", argv):
                with patch.object(node_setup.shutil, "which", return_value="/usr/bin/docker"):
                    with patch.object(node_setup, "preflight_origin"):
                        with patch.object(node_setup, "prepare_env", return_value=False):
                            with patch.object(node_setup.subprocess, "run"):
                                with patch.object(node_setup, "_create_cli_token", return_value="secret-token") as mint:
                                    with patch.object(node_setup, "probe", return_value=("Node", 1)):
                                        self.assertEqual(node_setup.main(), 0)
                                    mint.assert_called_once()
            data = json.loads(config.read_text())
            self.assertEqual(data["token"], "secret-token")
            self.assertEqual(data["node_url"], "https://node.example")
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_start_skips_cli_token_with_flag(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            argv = [
                "node_setup.py",
                "start",
                "--private",
                "--no-cli-token",
                "--cli-config",
                str(config),
            ]
            with patch.object(sys, "argv", argv):
                with patch.object(node_setup.shutil, "which", return_value="/usr/bin/docker"):
                    with patch.object(node_setup, "prepare_env", return_value=False):
                        with patch.object(node_setup.subprocess, "run"):
                            with patch.object(node_setup, "_create_cli_token") as mint:
                                with patch.object(node_setup, "probe", return_value=("Node", 0)):
                                    self.assertEqual(node_setup.main(), 0)
                                mint.assert_not_called()
            self.assertFalse(config.exists())

    def test_write_cli_config_merges_and_chmods(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "cfg" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"keep": "me", "node_url": "http://old"}))
            node_setup._write_cli_config(config, "https://node.example", "tok")
            data = json.loads(config.read_text())
            self.assertEqual(
                data,
                {"keep": "me", "node_url": "https://node.example", "token": "tok"},
            )
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)

    def test_first_boot_compose_is_loopback_with_auto_identity(self) -> None:
        config = (SCRIPT.parent.parent / "compose.node.yml").read_text()
        self.assertIn('"127.0.0.1:${ROUTSTR_NODE_PORT:-8000}:8000"', config)
        self.assertIn('AUTO_GENERATE_NSEC: "true"', config)
        self.assertIn('ENABLE_ANALYTICS_SHARING: "false"', config)
        self.assertNotIn("HS_ROUTER", config)


if __name__ == "__main__":
    unittest.main()
