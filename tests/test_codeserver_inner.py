import json
import os
import pathlib
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codeserver_inner
import codeserver_status

cs_loader = SourceFileLoader("cs", str(ROOT / "cs"))
cs_spec = spec_from_loader("cs", cs_loader)
cs = module_from_spec(cs_spec)
assert cs_spec.loader is not None
cs_spec.loader.exec_module(cs)


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

    def test_cleanup_is_suppressed_during_upgrade_grace(self):
        with mock.patch.object(
            codeserver_inner, "protected_server_commits"
        ) as protected:
            codeserver_inner.cleanup_stale_server_processes(
                "code",
                os.environ.copy(),
                upgrade_grace_until=20.0,
                now=10.0,
            )

        protected.assert_not_called()


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


class RelayReadinessTests(unittest.TestCase):
    def test_parses_connected_code_tunnel_status(self):
        status = codeserver_inner.parse_tunnel_status(
            json.dumps(
                {
                    "tunnel": {
                        "name": "misha",
                        "tunnel": "Connected",
                        "last_fail_reason": None,
                    },
                    "service_installed": False,
                }
            )
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertTrue(status.connected)
        self.assertIn("name=misha", status.summary)
        self.assertIn("state=Connected", status.summary)

    def test_parses_disconnected_code_tunnel_status(self):
        status = codeserver_inner.parse_tunnel_status(
            json.dumps(
                {
                    "tunnel": {
                        "name": "misha",
                        "tunnel": "Disconnected",
                        "last_fail_reason": "network",
                    }
                }
            )
        )

        self.assertIsNotNone(status)
        assert status is not None
        self.assertFalse(status.connected)
        self.assertIn("last_fail_reason=network", status.summary)

    def test_detects_upgrade_hints(self):
        self.assertTrue(
            codeserver_inner.has_upgrade_hint(
                "[rpc.0] Updating CLI to 1.123.0 (commit abc123)"
            )
        )
        self.assertTrue(codeserver_inner.has_upgrade_hint("respawn requested"))

    def test_status_polling_drives_relay_readiness_for_code_jobs(self):
        old_status_interval = codeserver_inner.TUNNEL_STATUS_CHECK_INTERVAL_SECONDS
        old_stale_interval = codeserver_inner.STALE_SERVER_CHECK_INTERVAL_SECONDS
        codeserver_inner.TUNNEL_STATUS_CHECK_INTERVAL_SECONDS = 0.05
        codeserver_inner.STALE_SERVER_CHECK_INTERVAL_SECONDS = 0.05
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = pathlib.Path(tmp)
                fake_code = tmp_path / "fake-code"
                fake_code.write_text(
                    """#!/usr/bin/env bash
if [ "$1" = "--version" ]; then
  echo "code 1.123.2 (commit 3c631b164c239e7aeaaae7c626b46c527b361af2)"
elif [ "$1" = "tunnel" ] && [ "$2" = "status" ]; then
  echo '{"tunnel":{"name":"misha","tunnel":"Connected","last_fail_reason":null},"service_installed":false}'
else
  exit 2
fi
""",
                    encoding="utf-8",
                )
                fake_code.chmod(0o755)
                log = tmp_path / "tunnel.log"

                with mock.patch.object(
                    codeserver_inner, "cancel_previous_job"
                ) as cancel, mock.patch.object(
                    codeserver_inner, "cleanup_stale_server_processes"
                ) as cleanup:
                    rc, respawn = codeserver_inner.forward_pty_output(
                        ["bash", "-lc", "sleep 0.2"],
                        os.environ.copy(),
                        log,
                        "12345",
                        5,
                        str(fake_code),
                    )

                self.assertEqual(rc, 0)
                self.assertFalse(respawn)
                cancel.assert_called_once_with("12345")
                cleanup.assert_not_called()
        finally:
            codeserver_inner.TUNNEL_STATUS_CHECK_INTERVAL_SECONDS = old_status_interval
            codeserver_inner.STALE_SERVER_CHECK_INTERVAL_SECONDS = old_stale_interval


class SlurmSelectionTests(unittest.TestCase):
    def test_prefers_running_job_over_pending_matches(self):
        matches = [
            {"job_id": "1993436", "state": "PENDING"},
            {"job_id": "1993435", "state": "RUNNING"},
            {"job_id": "1993437", "state": "PENDING"},
        ]

        self.assertEqual(cs.preferred_job_id(matches), "1993435")


class RelayProgressTests(unittest.TestCase):
    def test_session_progress_reports_running_remaining_time(self):
        meta = {
            "job_id": "333",
            "duration_seconds": 8 * 60 * 60,
        }

        with mock.patch.object(
            codeserver_status,
            "query_job_progress",
            return_value=("RUNNING", 24 * 60, 8 * 60 * 60),
        ):
            requested, used, remaining = codeserver_status.session_progress(meta)

        self.assertEqual(requested, 8 * 60 * 60)
        self.assertEqual(used, 24 * 60)
        self.assertEqual(remaining, (7 * 60 * 60) + (36 * 60))

    def test_relay_progress_uses_segment_begin_plus_elapsed(self):
        chain = {
            "requested_time_seconds": 10 * 60 * 60,
            "jobs": [
                {
                    "job_id": "111",
                    "begin_offset_seconds": 0,
                    "duration_seconds": 8 * 60 * 60,
                },
                {
                    "job_id": "222",
                    "begin_offset_seconds": (7 * 60 * 60) + (45 * 60),
                    "duration_seconds": (2 * 60 * 60) + (15 * 60),
                },
            ],
        }

        def fake_progress(job_id):
            if job_id == "111":
                return "COMPLETED", 8 * 60 * 60, 8 * 60 * 60
            if job_id == "222":
                return "RUNNING", 30 * 60, (2 * 60 * 60) + (15 * 60)
            raise AssertionError(job_id)

        with mock.patch.object(codeserver_status, "query_job_progress", fake_progress):
            used, remaining = codeserver_status.relay_progress(chain)

        self.assertEqual(used, (8 * 60 * 60) + (15 * 60))
        self.assertEqual(remaining, (1 * 60 * 60) + (45 * 60))


if __name__ == "__main__":
    unittest.main()
