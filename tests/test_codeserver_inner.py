import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codeserver_inner


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
