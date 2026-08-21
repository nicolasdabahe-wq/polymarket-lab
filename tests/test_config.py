"""El bot jamás debe operar en real sin el valor mágico exacto."""
from pmbot.config import LIVE_TRADING_MAGIC, is_live_trading


def test_default_is_paper():
    assert is_live_trading({}) is False


def test_empty_value_is_paper():
    assert is_live_trading({"LIVE_TRADING": ""}) is False


def test_truthy_but_wrong_values_are_paper():
    for value in ("1", "true", "yes", "on", "live", "LIVE",
                  "i_understand_the_risks", " I_UNDERSTAND_THE_RISKS"):
        assert is_live_trading({"LIVE_TRADING": value}) is False, value


def test_exact_magic_enables_live():
    assert is_live_trading({"LIVE_TRADING": LIVE_TRADING_MAGIC}) is True
