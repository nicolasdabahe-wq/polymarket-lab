from pmbot.intel.analyzer import (MarketRef, extract_json, keyword_candidates,
                                  tokenize)


def m(cid: str, question: str, category: str = "politics") -> MarketRef:
    return MarketRef(cid, question, category, 0.5)


MARKETS = [
    m("0x1", "Will the Fed decrease interest rates by 25 bps in September?",
      "economy"),
    m("0x2", "Will Bitcoin reach $100,000 in August?", "crypto"),
    m("0x3", "Will Donald Trump win the 2028 US Presidential Election?"),
    m("0x4", "Dota 2: Iron Wing vs BoomBoys", "sports"),
]


def test_fed_news_maps_to_fed_market():
    result = keyword_candidates(
        "Fed expected to cut interest rates in September meeting",
        "Traders bet on a 25 bps decrease", MARKETS)
    assert result and result[0][0].condition_id == "0x1"


def test_bitcoin_news_maps_to_bitcoin_market():
    result = keyword_candidates(
        "Bitcoin surges past $95,000 toward $100,000", "", MARKETS)
    assert result and result[0][0].condition_id == "0x2"


def test_unrelated_news_maps_nothing():
    result = keyword_candidates("Local bakery wins pastry award",
                                "Croissants praised by judges", MARKETS)
    assert result == []


def test_single_token_overlap_not_enough():
    # "Will" es stopword-like; una sola palabra común no debe matchear.
    result = keyword_candidates("Trump", "", MARKETS, min_overlap=2)
    assert result == []


def test_title_tokens_weigh_double():
    title_hit = keyword_candidates(
        "Bitcoin reach new highs", "", MARKETS)
    summary_hit = keyword_candidates(
        "Crypto markets news roundup", "Bitcoin may reach new highs", MARKETS)
    assert title_hit[0][1] > summary_hit[0][1]


def test_tokenize_removes_stopwords():
    tokens = tokenize("Will the Fed decrease rates?")
    assert "the" not in tokens and "will" not in tokens
    assert "fed" in tokens and "decrease" in tokens


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_with_prose_and_nesting():
    text = 'Claro, aquí está:\n{"relevant": true, "markets": [{"x": {"y": 2}}]}\nlisto'
    assert extract_json(text)["markets"][0]["x"]["y"] == 2


def test_extract_json_invalid():
    assert extract_json("no hay json acá") is None
    assert extract_json("{roto") is None
