import pytest
from jinja2 import UndefinedError

from runrail.worker.templating import render


def test_renders_builtin_and_parameters():
    assert render("daily_{{ ds }}_{{ region }}", {"ds": "2026-06-01", "region": "ca"}) == "daily_2026-06-01_ca"


def test_missing_value_is_an_error():
    with pytest.raises(UndefinedError): render("{{ missing }}", {})

