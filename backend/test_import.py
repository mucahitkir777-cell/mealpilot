import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import server


class MealPilotImportTests(unittest.TestCase):
    def test_caption_recipe_is_complete_without_ai(self):
        caption = (
            "Protein Porridge mit Beeren\nZutaten:\n50 g Haferflocken\n"
            "30 g Whey\n100 g Beeren\n"
            "Zubereitung:\nAlle Zutaten verrühren und über Nacht im Kühlschrank quellen lassen."
        )
        recipe = server.recipe_from_caption(caption)
        self.assertTrue(server.is_complete(recipe))
        self.assertEqual(recipe["category"], "Frühstück")
        self.assertEqual(len(recipe["ingredients"]), 3)
        self.assertEqual(recipe["ingredients"][0]["grams"], 50)

    def test_unknown_spoon_quantity_is_not_guessed(self):
        ingredient = server.parse_ingredient("2 EL Honig")
        self.assertEqual(ingredient["grams"], 0)
        self.assertTrue(ingredient["uncertain"])

    def test_caption_first_does_not_call_gemini_or_video(self):
        caption = (
            "Protein Porridge\n50 g Haferflocken\n30 g Whey\n100 g Beeren\n"
            "Zubereitung: Alles gut umrühren und anschließend 10 Minuten quellen lassen."
        )
        meta = {"caption": caption, "thumbnail": "", "author": "Demo"}
        with patch.object(server, "embed", return_value=meta), \
                patch.object(server, "fetch_video_metadata", return_value={}), \
                patch.object(server, "gemini_extract", side_effect=AssertionError("Gemini called")), \
                patch.object(server, "load_public_video", side_effect=AssertionError("Video called")):
            client = server.app.test_client()
            result = client.post("/api/import",
                                 json={"url": "https://www.tiktok.com/@demo/video/123456"},
                                 headers={"Origin": server.FRONTEND})
        self.assertEqual(result.status_code, 200)
        data = result.get_json()
        self.assertEqual(data["extraction_method"], "caption")
        self.assertFalse(data["video_processed"])
        self.assertEqual(len(data["recipe"]["ingredients"]), 3)

    def test_untrusted_non_tiktok_url_rejected(self):
        self.assertFalse(server.ok_tiktok_url("https://evil.tiktok.com.evil.org/"))
        self.assertFalse(server.ok_tiktok_url("http://www.tiktok.com/@demo/video/1"))
        self.assertTrue(server.ok_tiktok_url("https://vm.tiktok.com/abcdef/"))


if __name__ == "__main__":
    unittest.main()
