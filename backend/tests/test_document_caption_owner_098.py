"""Declared vocabulary and real HTML-grid ownership, without provider calls."""
import pytest
from test_document_header_association_098 import block, field, page

from trackvance.document_header_association_v2 import header_observations_v2
from trackvance.document_lossless import _resolve


def resolved(config, source):
    observations, traces = header_observations_v2([source], [config])
    return _resolve(observations[config["name"]], config), traces[config["name"]]


@pytest.mark.parametrize("caption,currency", [("Total a cobrar", "EUR"), ("Total to collect", "USD")])
def test_one_amount_after_explicit_empty_html_cells_has_declared_caption_owner(caption,currency):
    config = field("total", "Total", logical_type="DECIMAL", decimal_separator=",", thousands_separator=".",
                   decimal_format_explicit=True, currency_symbols=[currency])
    source = page(html=f"<table><tr><td>{caption} {currency}</td><td></td><td>219,75</td></tr></table>")
    cell,traces = resolved(config,source)
    assert cell["value"] == "219.75" and cell["validity"] == "VALID"
    assert any(item["observed"] == "219,75" and item["status"] == "PROVEN" and item["value_refs"] for item in traces)


@pytest.mark.parametrize("name,caption,value", [
    ("account", "Identificación de cuenta", "AX-6203"),
    ("account", "Account identification", "BX-8032"),
    ("holder", "Titular registrado", "Cooperativa Río Azul"),
    ("holder", "Registered holder", "Blue River Cooperative"),
])
def test_compound_nonmonetary_caption_owns_its_value_without_short_caption_suffix(name,caption,value):
    config = field(name,name.title())
    cell,traces = resolved(config,page(block(f"{caption}: {value}")))
    assert cell["value"] == value and cell["validity"] == "VALID"
    assert all(item["observed"] == value or not item["eligible_for_field"] for item in traces)


@pytest.mark.parametrize("html", [
    "<tr><td>Total a cobrar EUR</td><td></td><td>219,75</td><td>224,00</td></tr>",
    "<tr><td>Total a cobrar EUR</td><td>Referencia</td><td>219,75</td></tr>",
    "<tr><td>Total a cobrar EUR</td><td colspan='2'></td></tr><tr><td></td><td></td><td>219,75</td></tr>",
])
def test_multiple_values_intervening_caption_or_missing_slot_cannot_be_skipped(html):
    config = field("total", "Total", logical_type="DECIMAL", decimal_separator=",", thousands_separator=".",
                   decimal_format_explicit=True, currency_symbols=["EUR"])
    cell,_ = resolved(config,page(html="<table>"+html+"</table>"))
    assert cell["validity"] != "VALID"
