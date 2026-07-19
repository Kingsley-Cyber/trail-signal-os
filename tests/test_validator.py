import unittest
from niche_research.paths import repo_root
from niche_research.validator import validate_repository


class ValidatorTests(unittest.TestCase):
    def test_repository_is_valid(self):
        result = validate_repository(repo_root())
        self.assertTrue(result.ok, "\n".join(result.errors))
        self.assertGreaterEqual(result.stats["data/outdoor_activity_niche_seed.csv"], 1000)


if __name__ == "__main__":
    unittest.main()
