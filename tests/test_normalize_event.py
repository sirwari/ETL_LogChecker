from etl.event import Event as EtlEvent
from etl_logchecker import ETLAnalyzer


class MockGuid:
    def __init__(self) -> None:
        self.data1 = 0x22FB2CD6
        self.data2 = 0x0E7B
        self.data3 = 0x422B
        self.data4 = [0xA0, 0xC7, 0x2F, 0xAD, 0x1F, 0xD0, 0xE7, 0x16]


class MockEventHeader:
    def __init__(self) -> None:
        self.provider_id = MockGuid()
        self.process_id = 4242
        self.timestamp = 123456


class MockSource:
    def __init__(self) -> None:
        self.event_header = MockEventHeader()


def test_normalize_etl_event_schema_guid():
    analyzer = ETLAnalyzer("dummy.etl", use_tracerpt=False)
    event = EtlEvent(MockSource())
    normalized = analyzer._normalize_event(event)
    assert normalized["Timestamp"] == 123456
    assert normalized["ProcessID"] == 4242
    assert (
        normalized["ProviderGuid"]
        == "22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716"
    )
