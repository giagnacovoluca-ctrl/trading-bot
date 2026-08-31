import subprocess
import unittest
from unittest.mock import patch

import agente_tiktok


class AgyValidatorRetryTest(unittest.TestCase):
    @patch("agente_tiktok.time.sleep")
    @patch("agente_tiktok.subprocess.run")
    def test_retries_503_then_succeeds(self, run_mock, sleep_mock):
        run_mock.side_effect = [
            subprocess.CompletedProcess(["agy"], 1, "", "UNAVAILABLE (code 503)"),
            subprocess.CompletedProcess(["agy"], 0, "SICURO: SI", ""),
        ]

        result = agente_tiktok._run_agy_validator("prompt")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(5)

    @patch("agente_tiktok.time.sleep")
    @patch("agente_tiktok.subprocess.run")
    def test_stops_after_three_service_failures(self, run_mock, sleep_mock):
        run_mock.return_value = subprocess.CompletedProcess(
            ["agy"], 1, "", "UNAVAILABLE (code 503)"
        )

        result = agente_tiktok._run_agy_validator("prompt")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(run_mock.call_count, 3)
        self.assertEqual(sleep_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
