import unittest
from pathlib import Path
from unittest.mock import patch

from modules.ebook_catalog import ebook_to_rag, get_ebook, load_ebook_catalog, video_cta_copy
import rag_generator


class EbookCatalogTest(unittest.TestCase):
    def test_catalog_has_three_pdf_and_three_preview_resources(self):
        catalog = load_ebook_catalog()
        pdf = [book for book in catalog if book["deliveryType"] == "pdf_email"]
        previews = [book for book in catalog if book["deliveryType"] == "preview_online"]
        self.assertEqual(len(catalog), 6)
        self.assertEqual({book["id"] for book in pdf}, {"acqua", "epigenetica", "nervo-vago"})
        self.assertEqual({book["id"] for book in previews}, {"meditazione", "cibo", "integratori"})

    def test_catalog_destinations_and_source_files_exist(self):
        for book in load_ebook_catalog():
            expected_prefix = "/scarica/" if book["deliveryType"] == "pdf_email" else "/regalo/"
            self.assertTrue(book["landingPath"].startswith(expected_prefix))
            self.assertTrue((Path("/home/ubuntu/ebooks") / book["sourceFile"]).exists())
            if book["deliveryType"] == "pdf_email":
                self.assertTrue((Path("/home/ubuntu/conscia-mente/public/freebies") / book["pdfFile"]).exists())

    def test_rag_mapping_keeps_delivery_specific_cta(self):
        pdf = ebook_to_rag(get_ebook("acqua"))
        preview = ebook_to_rag(get_ebook("meditazione"))
        self.assertIn("PDF", pdf["cta_tiktok"])
        self.assertIn("anteprima", preview["cta_tiktok"])

    def test_video_cta_never_replaces_the_free_resource_with_amazon(self):
        for ebook in load_ebook_catalog():
            title, detail, action = video_cta_copy(ebook)
            combined = f"{title} {detail} {action}".lower()
            self.assertNotIn("amazon", combined)
            self.assertIn("link in bio", combined)
            expected = "pdf" if ebook["deliveryType"] == "pdf_email" else "anteprima"
            self.assertIn(expected, combined)

    def test_promo_selection_starts_from_an_ebook(self):
        with patch.object(rag_generator.random, "choices", return_value=[rag_generator.find_ebook_by_id("epigenetica")]), patch.object(
            rag_generator.random,
            "choice",
            return_value="La differenza tra DNA ed espressione genica",
        ):
            category, topic, ebook_id = rag_generator.pick_promo_ebook_topic()
        self.assertEqual(category, "Ebook/epigenetica")
        self.assertEqual(ebook_id, "epigenetica")
        self.assertIn("DNA", topic)


if __name__ == "__main__":
    unittest.main()
