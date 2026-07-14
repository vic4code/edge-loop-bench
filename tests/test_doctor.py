from __future__ import annotations

import unittest
from unittest.mock import patch

from edgeloopbench.doctor import collect_host_info


class DoctorTests(unittest.TestCase):
    @patch("edgeloopbench.doctor.platform.processor", return_value="test-cpu")
    @patch("edgeloopbench.doctor.platform.system", return_value="Linux")
    def test_non_macos_host_does_not_run_sysctl(self, _system, _processor) -> None:
        commands: list[list[str]] = []

        def runner(command: list[str]) -> str | None:
            commands.append(command)
            return None

        result = collect_host_info(runner)

        self.assertEqual(result["chip"], "test-cpu")
        self.assertEqual(commands, [])

    @patch("edgeloopbench.doctor.platform.system", return_value="Darwin")
    def test_failed_macos_discovery_is_reported_as_unavailable(self, _system) -> None:
        result = collect_host_info(lambda _command: None)

        self.assertIsNone(result["chip"])
        self.assertIsNone(result["memory_bytes"])
        self.assertIsNone(result["logical_cpu_count"])


if __name__ == "__main__":
    unittest.main()
