"""An unsplit measured footer must not become the last detail row's amount."""
import copy
import importlib.util
import os
import unittest
from pathlib import Path

ENGINE_PATH = Path(os.environ.get("TABLE_STOP_ENGINE", Path(__file__).resolve().parents[1] / "src/trackvance/document_engine.py"))
SPEC = importlib.util.spec_from_file_location("table_stop_engine", ENGINE_PATH)
ENGINE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENGINE)
RULES = ENGINE._visual_rules


def line(text, box, page=1, measured=None):
    return {"text": text, "region": box, "page": page, "method": "OCR", "confidence": None,
            "words": measured if measured is not None else [{"text": text, "region": list(box), "confidence": None}]}


def fixture():
    grammar = {"column": 0, "separator": "-", "segments": [
        {"alphabet": "ASCII_ALPHA", "minimum": 2, "maximum": 4},
        {"alphabet": "ASCII_DIGIT", "minimum": 5, "maximum": 5},
        {"alphabet": "ASCII_DIGIT", "minimum": 2, "maximum": 2}], "maximum_start_inset": .035}
    rule = {"page": 1, "region": [.02, .18, .98, .23], "header_labels": ["ITEM", "QTY", "AMOUNT"],
            "column_bounds": [.02, .55, .7, .98], "stop_label": "FIN DEL DETALLE",
            "stop_label_aliases": ["FINAL DE REGISTROS"], "flowing": True, "continue_pages": True,
            "multiline_column": 0, "row_tolerance": .027, "row_start": grammar}
    definition = {"fields": [], "tables": [{"name": "detail", "fields": [
        {"name": "item", "logical_type": "STRING"}, {"name": "qty", "logical_type": "DECIMAL"},
        {"name": "amount", "logical_type": "DECIMAL"}], "visual": rule}]}
    lines = []
    for page in (1, 2):
        header_words = [{"text": text, "region": box, "confidence": None} for text, box in (
            ("ITEM", [.03, .2, .2, .22]), ("QTY", [.56, .2, .65, .22]), ("AMOUNT", [.72, .2, .9, .22]))]
        lines.append(line("ITEM QTY AMOUNT", [.03, .2, .9, .22], page, header_words))
        for row_index, top in enumerate((.32, .55)):
            key = f"AB-{page * 100 + row_index:05d}-01"
            lines.append(line(key, [.03, top, .2, top + .015], page))
            words = [{"text": text, "region": box, "confidence": None} for text, box in (
                ("Measured work", [.03, top + .025, .4, top + .045]),
                ("3,50", [.57, top + .025, .64, top + .045]),
                ("27,65", [.83, top + .025, .94, top + .045]))]
            lines.append(line("Measured work 3,50 27,65", [.03, top + .025, .94, top + .045], page, words))
        lines.append(line("FIN DEL DETALLE | COPIA SIN VALOR", [.02, .9, .5, .92], page))
        lines.append(line("FOLIO 7", [.82, .9, .96, .92], page))
    return lines, definition


class MeasuredFooterStopTests(unittest.TestCase):
    def test_exact_pipe_footer_preserves_all_rows_and_next_page(self):
        lines, definition = fixture()
        prior = copy.deepcopy(lines)
        result = ENGINE.extract(lines, definition)
        self.assertEqual(len(result["tables"]["detail"]), 4)
        self.assertEqual(result["table_unparsed"]["detail"], [])
        self.assertEqual([row["qty"]["observed"] for row in result["tables"]["detail"]], ["3,50"] * 4)
        self.assertEqual([row["amount"]["observed"] for row in result["tables"]["detail"]], ["27,65"] * 4)
        self.assertTrue(all(not cell["reviewed"] for row in result["tables"]["detail"] for cell in row.values()))
        self.assertEqual(lines, prior)
        self.assertEqual(result["lines"], prior)  # No footer/source evidence was deleted.

    def test_prefix_fallback_keeps_source_rectangle(self):
        lines, definition = fixture()
        footer = lines[-2]
        prior = copy.deepcopy(footer)
        self.assertTrue(RULES.declared_table_stop(footer, "FIN DEL DETALLE", definition["tables"][0]["visual"]))
        self.assertEqual(footer, prior)

    def test_no_fuzzy_substring_or_undeclared_caption(self):
        _, definition = fixture()
        rule = definition["tables"][0]["visual"]
        for text in ("PREFIX FIN DEL DETALLE | COPIA", "FIN DEL DETALLE EXTRA | COPIA",
                     "FIN DEL DETALLE COPIA", "FIN DEL DETALE | COPIA", "UNDECLARED | COPIA"):
            with self.subTest(text=text):
                self.assertFalse(RULES.declared_table_stop(line(text, [.02, .9, .6, .92]), "FIN DEL DETALLE", rule))

    def test_business_key_anywhere_in_pipe_block_blocks_new_fallback(self):
        _, definition = fixture()
        rule = definition["tables"][0]["visual"]
        for separator in (" ", "|", "\\", "\n"):
            with self.subTest(separator=separator):
                self.assertFalse(RULES.declared_table_stop(
                    line("FIN DEL DETALLE | COPIA" + separator + "AB-12345-01", [.02, .9, .8, .92]),
                    "FIN DEL DETALLE", rule))

    def test_nonleft_block_does_not_create_stop(self):
        _, definition = fixture()
        rule = definition["tables"][0]["visual"]
        observed = line("work FIN DEL DETALLE | COPIA", [.02, .9, .8, .92], measured=[
            {"text": "work", "region": [.02, .9, .2, .92]},
            {"text": "FIN DEL DETALLE | COPIA", "region": [.4, .9, .8, .92]}])
        self.assertFalse(RULES.declared_table_stop(observed, "FIN DEL DETALLE", rule))

    def test_rules_without_declared_row_grammar_keep_historical_behavior(self):
        _, definition = fixture()
        rule = definition["tables"][0]["visual"]
        del rule["row_start"]
        self.assertFalse(RULES.declared_table_stop(line("FIN DEL DETALLE | COPIA", [.02, .9, .8, .92]),
                                                 "FIN DEL DETALLE", rule))

    def test_original_exact_word_anchor_and_declared_alias_remain_supported(self):
        _, definition = fixture()
        rule = definition["tables"][0]["visual"]
        self.assertTrue(RULES.declared_table_stop(line("FIN DEL DETALLE", [.02, .9, .5, .92]), "FIN DEL DETALLE", rule))
        self.assertTrue(RULES.declared_table_stop(line("FINAL DE REGISTROS | COPIA", [.02, .9, .5, .92]),
                                                rule["stop_label_aliases"][0], rule))


if __name__ == "__main__":
    unittest.main()
