from __future__ import annotations

import hashlib
import unittest

from edgeloopbench.intercode_source import load_intercode_source
from edgeloopbench.intercode_v07_protocol import (
    V07_SAMPLE_MANIFEST_SHA256,
    V07_TASK_IDS,
    build_v07_sample,
    candidate_progress_evaluation,
)


class InterCodeV07ProtocolTests(unittest.TestCase):
    def test_rebuilds_the_preregistered_gold_free_sample(self) -> None:
        source = load_intercode_source()

        selected = build_v07_sample(source)

        self.assertEqual(selected, V07_TASK_IDS)
        self.assertEqual(len(selected), 30)
        self.assertEqual(len(set(selected)), 30)
        payload = "".join(f"{task_id}\n" for task_id in selected).encode("ascii")
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            V07_SAMPLE_MANIFEST_SHA256,
        )

    def test_candidate_progress_is_bounded_below_the_success_stop(self) -> None:
        evaluation = candidate_progress_evaluation(
            parsed_single_action=True,
            action_admissible=True,
            exit_code=0,
            state_changed=True,
            normalized_output_nonempty=True,
        )

        self.assertEqual(evaluation.reward, 0.8)
        self.assertFalse(evaluation.official_success)

    def test_candidate_progress_ranks_partial_candidate_surfaces(self) -> None:
        output_only = candidate_progress_evaluation(
            parsed_single_action=True,
            action_admissible=True,
            exit_code=2,
            state_changed=False,
            normalized_output_nonempty=True,
        )
        no_effect = candidate_progress_evaluation(
            parsed_single_action=True,
            action_admissible=True,
            exit_code=2,
            state_changed=False,
            normalized_output_nonempty=False,
        )

        self.assertEqual(output_only.reward, 0.6)
        self.assertEqual(no_effect.reward, 0.4)
        self.assertGreater(output_only.reward, no_effect.reward)
        self.assertFalse(output_only.official_success)
        self.assertFalse(no_effect.official_success)

    def test_progress_contract_rejects_contradictory_surface_facts(self) -> None:
        with self.assertRaisesRegex(ValueError, "unparsed"):
            candidate_progress_evaluation(
                parsed_single_action=False,
                action_admissible=True,
                exit_code=0,
                state_changed=True,
                normalized_output_nonempty=True,
            )
        with self.assertRaisesRegex(ValueError, "inadmissible"):
            candidate_progress_evaluation(
                parsed_single_action=True,
                action_admissible=False,
                exit_code=0,
                state_changed=False,
                normalized_output_nonempty=False,
            )


if __name__ == "__main__":
    unittest.main()
