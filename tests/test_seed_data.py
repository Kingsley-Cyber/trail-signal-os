import unittest
from niche_research.csv_store import read_rows
from niche_research.paths import repo_root


class SeedDataTests(unittest.TestCase):
    def test_seed_scale_and_unique_ids(self):
        rows = read_rows(repo_root() / "data/outdoor_activity_niche_seed.csv")
        self.assertGreaterEqual(len(rows), 1000)
        ids = [row["seed_id"] for row in rows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(row["fact_status"] == "hypothesis" for row in rows))

    def test_candidate_foreign_keys(self):
        root = repo_root()
        activities = {row["activity_id"] for row in read_rows(root / "data/outdoor_activity_niche_seed.csv")}
        candidates = read_rows(root / "data/niche_candidates.csv")
        self.assertGreaterEqual(len(candidates), 30)
        self.assertTrue(all(row["activity_id"] in activities for row in candidates))
        self.assertTrue(all(row["hard_gates_passed"] == "false" for row in candidates))


if __name__ == "__main__":
    unittest.main()
