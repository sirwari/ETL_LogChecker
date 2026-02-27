from etl_logchecker import _build_timeline_rows


def test_build_timeline_rows_sorted_and_duration():
    pid_start_ts = {1: 1.0, 2: 0.5}
    pid_end_ts = {1: 2.5}
    pid_info = {
        1: {"image": "proc1", "session_id": 1, "parent_id": 0, "command_line": "cmd1"},
        2: {"image": "proc2", "session_id": 2, "parent_id": 1, "command_line": "cmd2"},
    }
    pid_first_io_ts = {1: 1.2}
    pid_first_image_ts = {2: 0.6}
    trace_duration = 3.0

    rows = _build_timeline_rows(
        pid_start_ts,
        pid_end_ts,
        pid_info,
        pid_first_io_ts,
        pid_first_image_ts,
        trace_duration,
    )

    assert rows[0]["pid"] == 2
    assert rows[0]["duration_s"] == 2.5
    assert rows[1]["pid"] == 1
    assert rows[1]["duration_s"] == 1.5
    assert rows[1]["first_io_s"] == 1.2
