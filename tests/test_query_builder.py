import unittest
from niche_research.query_builder import generate_queries


class QueryBuilderTests(unittest.TestCase):
    def test_generation_and_deduplication(self):
        activity = [{
            "activity_id": "act-test", "seed_id": "seed-1", "activity": "Bank fishing",
            "task": "change lure", "friction_family": "small_parts",
            "product_territory": "tethered workstation",
        }]
        templates = [
            {"template_id": "q1", "evidence_goal": "complaint", "template": "{activity} {task} annoying"},
            {"template_id": "q2", "evidence_goal": "complaint", "template": "{activity} {task} annoying"},
        ]
        output = generate_queries(activity, templates, "United States", 2026)
        self.assertEqual(len(output), 1)
        self.assertIn("Bank fishing", output[0]["query"])

    def test_unknown_placeholder_fails(self):
        with self.assertRaises(ValueError):
            generate_queries([{"activity": "Fishing"}], [{"template_id": "bad", "template": "{missing}"}])


if __name__ == "__main__":
    unittest.main()
