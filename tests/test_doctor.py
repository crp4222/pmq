import pmq.doctor as doctor


def test_advise_sig_type_matrix():
    assert doctor.advise_sig_type(False, False, True)[0] == 0
    assert doctor.advise_sig_type(True, True, False)[0] == 3
    assert doctor.advise_sig_type(True, False, False)[0] is None
    assert doctor.advise_sig_type(False, False, False)[0] is None


def test_minimal_proxy_detector():
    assert doctor.looks_like_minimal_proxy("0x363d3d373d3d363d6020" + "ab" * 60)
    assert not doctor.looks_like_minimal_proxy("0x")
    assert not doctor.looks_like_minimal_proxy(None)
    assert not doctor.looks_like_minimal_proxy("0x" + "ab" * 300)


def test_main_without_key_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("POLY_PRIVATE_KEY", raising=False)
    assert doctor.main([]) == 1
    assert "POLY_PRIVATE_KEY" in capsys.readouterr().out


def test_main_market_flag_without_value_exits_2(capsys):
    assert doctor.main(["--market"]) == 2
    assert "usage" in capsys.readouterr().out
