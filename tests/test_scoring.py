import unittest
from pathlib import Path
from niche_research.scoring import load_scoring_config, score_candidate
from niche_research.paths import repo_root


class ScoringTests(unittest.TestCase):
    def test_max_score_and_gate(self):
        config = load_scoring_config(repo_root() / "config/scoring_weights.json")
        row = {key: 5 for key in config["dimensions"]}
        row.update({"hard_gates_passed": "true", "score_basis": "evidence_adjusted", "evidence_ids": "ev-1"})
        result = score_candidate(row, config)
        self.assertEqual(result["weighted_score"], "100.00")
        self.assertEqual(result["score_band"], "priority")
        self.assertEqual(result["experiment_eligible"], "true")

    def test_seed_prior_does_not_require_evidence(self):
        config = load_scoring_config(repo_root() / "config/scoring_weights.json")
        row = {key: 3 for key in config["dimensions"]}
        row.update({"hard_gates_passed": "false", "score_basis": "seed_prior", "evidence_ids": ""})
        result = score_candidate(row, config)
        self.assertEqual(result["weighted_score"], "60.00")
        self.assertEqual(result["experiment_eligible"], "false")
        self.assertEqual(result["penalties_applied"], "")

    def test_invalid_score_rejected(self):
        config = load_scoring_config(repo_root() / "config/scoring_weights.json")
        row = {key: 3 for key in config["dimensions"]}
        row["behavior_frequency"] = 8
        with self.assertRaises(ValueError):
            score_candidate(row, config)


if __name__ == "__main__":
    unittest.main()
