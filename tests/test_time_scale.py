import math
import pytest

from etl_logchecker import _infer_timestamp_scale, _parse_time_scale_arg


def test_parse_time_scale_units():
    assert _parse_time_scale_arg("auto") is None
    assert _parse_time_scale_arg("ns") == pytest.approx(1e-9)
    assert _parse_time_scale_arg("us") == pytest.approx(1e-6)
    assert _parse_time_scale_arg("ms") == pytest.approx(1e-3)
    assert _parse_time_scale_arg("s") == pytest.approx(1.0)
    assert _parse_time_scale_arg("1e-7") == pytest.approx(1e-7)


def test_parse_time_scale_invalid():
    with pytest.raises(ValueError):
        _parse_time_scale_arg("0")
    with pytest.raises(ValueError):
        _parse_time_scale_arg("-1")
    with pytest.raises(ValueError):
        _parse_time_scale_arg("nope")


def test_infer_timestamp_scale():
    ms_epoch = 1_600_000_000_000
    assert _infer_timestamp_scale(ms_epoch) == pytest.approx(1e-3)
    seconds_epoch = 1_600_000_000
    assert _infer_timestamp_scale(seconds_epoch) == pytest.approx(1.0)
    assert _infer_timestamp_scale(10_000_000) == pytest.approx(1e-3)
