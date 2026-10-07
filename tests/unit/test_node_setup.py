"""Offline contracts for the first-run node workflow."""

import importlib.util
import sys
import tempfile
import unittest
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

    def test_public_origin_only(self) -> None:
        self.assertEqual(node_setup.public_url("https://node.example/"), "https://node.example")
        for value in ("http://node.example", "https://name:pass@node.example", "https://node.example/admin", "https://node.example/?token=x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                node_setup.public_url(value)

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

    def test_start_passes_selected_port_to_compose(self) -> None:
        with patch.object(sys, "argv", ["node_setup.py", "start", "--port", "18080"]):
            with patch.object(node_setup.shutil, "which", return_value="/usr/bin/docker"):
                with patch.object(node_setup, "prepare_env", return_value=False):
                    with patch.object(node_setup.subprocess, "run") as run:
                        with patch.object(node_setup, "probe", return_value=("Node", 0)) as probe:
                            self.assertEqual(node_setup.main(), 0)
                            probe.assert_called_once_with("http://127.0.0.1:18080")
                            self.assertEqual(run.call_count, 2)
                            self.assertTrue(all(call.kwargs["env"]["ROUTSTR_NODE_PORT"] == "18080" for call in run.call_args_list))

    def test_first_boot_compose_is_private(self) -> None:
        config = (SCRIPT.parent.parent / "compose.node.yml").read_text()
        self.assertIn('"127.0.0.1:${ROUTSTR_NODE_PORT:-8000}:8000"', config)
        self.assertIn('AUTO_GENERATE_NSEC: "false"', config)
        self.assertIn('ENABLE_ANALYTICS_SHARING: "false"', config)
        self.assertNotIn("HS_ROUTER", config)


if __name__ == "__main__":
    unittest.main()
