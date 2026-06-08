import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codeserver_inner


class StaleServerCleanupTests(unittest.TestCase):
    def test_extracts_code_commit_from_version_output(self):
        text = "code 1.122.1 (commit 8761a5560cfd65fdd19ce7e2bd18dab5c0a4d84e)\n"

        self.assertEqual(
            codeserver_inner.extract_code_commit(text),
            "8761a5560cfd65fdd19ce7e2bd18dab5c0a4d84e",
        )

    def test_active_server_commit_reads_lru_first_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            servers_dir = pathlib.Path(tmp)
            (servers_dir / "lru.json").write_text(
                json.dumps(
                    [
                        "Stable-6a44c352bd24569c417e530095901b649960f9f8",
                        "Stable-8761a5560cfd65fdd19ce7e2bd18dab5c0a4d84e",
                    ]
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                codeserver_inner.active_server_commit(servers_dir),
                "6a44c352bd24569c417e530095901b649960f9f8",
            )

    def test_active_server_commit_missing_lru_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                codeserver_inner.active_server_commit(pathlib.Path(tmp))
            )

    def test_live_server_is_never_stale(self):
        # Regression: the freshly auto-updated server must be protected even
        # though it differs from the commit seen when the tunnel first launched.
        live = "6a44c352bd24569c417e530095901b649960f9f8"
        old = "8761a5560cfd65fdd19ce7e2bd18dab5c0a4d84e"
        protected = {live, old}

        self.assertFalse(codeserver_inner.is_stale_server_commit(live, protected))
        self.assertTrue(
            codeserver_inner.is_stale_server_commit(
                "f6cfa2ea2403534de03f069bdf160d06451ed282", {live}
            )
        )

    def test_staging_and_unknown_state_are_never_reaped(self):
        live = "6a44c352bd24569c417e530095901b649960f9f8"
        # Staging builds are skipped.
        self.assertFalse(
            codeserver_inner.is_stale_server_commit("abc123.staging", {live})
        )
        # With no known live commit we must not reap anything.
        self.assertFalse(codeserver_inner.is_stale_server_commit(live, set()))


class RespawnSupervisorTests(unittest.TestCase):

    def test_relaunches_after_respawn_request(self):
        old_delay = codeserver_inner.RESPAWN_RESTART_DELAY_SECONDS
        old_grace = codeserver_inner.TERMINATE_GRACE_SECONDS
        codeserver_inner.RESPAWN_RESTART_DELAY_SECONDS = 0
        codeserver_inner.TERMINATE_GRACE_SECONDS = 0.1
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = pathlib.Path(tmp)
                marker = tmp_path / "marker"
                log = tmp_path / "tunnel.log"
                cmd = (
                    f"if [ ! -f {marker} ]; then "
                    f"touch {marker}; echo respawn requested; sleep 60; "
                    "else echo second run ok; exit 0; fi"
                )

                rc = codeserver_inner.supervise_pty_output(
                    ["bash", "-lc", cmd],
                    os.environ.copy(),
                    log,
                    None,
                    1,
                )

                self.assertEqual(rc, 0)
                text = log.read_text(encoding="utf-8")
                self.assertIn("respawn requested", text)
                self.assertIn("second run ok", text)
        finally:
            codeserver_inner.RESPAWN_RESTART_DELAY_SECONDS = old_delay
            codeserver_inner.TERMINATE_GRACE_SECONDS = old_grace

    def test_repeated_respawns_are_capped(self):
        old_delay = codeserver_inner.RESPAWN_RESTART_DELAY_SECONDS
        old_grace = codeserver_inner.TERMINATE_GRACE_SECONDS
        codeserver_inner.RESPAWN_RESTART_DELAY_SECONDS = 0
        codeserver_inner.TERMINATE_GRACE_SECONDS = 0.1
        try:
            with tempfile.TemporaryDirectory() as tmp:
                log = pathlib.Path(tmp) / "tunnel.log"

                rc = codeserver_inner.supervise_pty_output(
                    ["bash", "-lc", "echo respawn requested; sleep 60"],
                    os.environ.copy(),
                    log,
                    None,
                    1,
                )

                self.assertEqual(rc, 1)
                text = log.read_text(encoding="utf-8")
                self.assertEqual(text.lower().count("respawn requested"), 4)
        finally:
            codeserver_inner.RESPAWN_RESTART_DELAY_SECONDS = old_delay
            codeserver_inner.TERMINATE_GRACE_SECONDS = old_grace


if __name__ == "__main__":
    unittest.main()
