"""
Unit tests for orchestrator/pdf_report.py: the Markdown-cleanup helpers
and a full render smoke test. No AWS calls (pure Python + reportlab).

Run directly: python3 tests/test_pdf_report_unit.py
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORCH_DIR = os.path.join(ROOT, "orchestrator")
sys.path.insert(0, ORCH_DIR)

import pdf_report as pdf  # noqa: E402


class CleanModelMarkdownTests(unittest.TestCase):

    def test_strips_wrapping_code_fence(self):
        text = "```markdown\n# Title\n\nBody text.\n```"
        self.assertEqual(pdf.clean_model_markdown(text), "# Title\n\nBody text.")

    def test_leaves_unfenced_text_alone(self):
        text = "# Title\n\nBody text."
        self.assertEqual(pdf.clean_model_markdown(text), text)

    def test_widens_two_space_nested_list_indent(self):
        text = "- top\n  - nested\n"
        result = pdf.clean_model_markdown(text)
        self.assertIn("\n    - nested", result)

    def test_leaves_four_space_indent_alone(self):
        text = "- top\n    - nested\n"
        self.assertEqual(pdf.clean_model_markdown(text), text.strip())


class DropTitleLineTests(unittest.TestCase):

    def test_drops_h1_naming_the_company(self):
        text = "# Acme Inc Report\n\nBody."
        self.assertEqual(pdf._drop_title_line(text, "Acme Inc"), "Body.")

    def test_keeps_h1_that_is_a_real_section(self):
        text = "# Executive Summary\n\nBody."
        self.assertEqual(pdf._drop_title_line(text, "Acme Inc"), text)

    def test_empty_text(self):
        self.assertEqual(pdf._drop_title_line("", "Acme"), "")


class RenderReportPdfSmokeTest(unittest.TestCase):

    def test_produces_a_valid_pdf(self):
        report = (
            "# Acme Inc Report\n\n"
            "## Executive facts\n"
            "- Acme Inc builds widgets. [S1]\n\n"
            "## Funding\n"
            "| Round | Amount | Valuation |\n"
            "|---|---|---|\n"
            "| Seed | Not publicly disclosed | ₹10 crore |\n"
        )
        pdf_bytes = pdf.render_report_pdf(
            report,
            "Acme Inc",
            key_facts=[("Round", "Seed"), ("Amount raised", "Not publicly disclosed")],
            sources=[{"id": "S1", "url": "https://acme.com", "domain": "acme.com",
                      "verified": True}],
            warnings=["Example data-quality note."],
        )
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
        self.assertGreater(len(pdf_bytes), 1000)

    def test_renders_even_with_no_optional_fields(self):
        pdf_bytes = pdf.render_report_pdf("# Acme Inc\n\nBody only.", "Acme Inc")
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
