"""Retention must preserve flight evidence and avoid reusing retained filenames."""

from pathlib import Path

import pytest

from log_management import cleanup_unarmed_logs, next_log_path


@pytest.mark.parametrize('event', [
    'Waiting for arming...',
    'Vehicle armed 1',
    'Vehicle armed True',
    'Arming motors',
    'Arm: Throttle too high',
    'takeoff mode',
    'Take off to 5.0m',
    'Flight-controller COMMAND_ACK: sysid=42 compid=1 '
    'command=MAV_CMD_COMPONENT_ARM_DISARM (400) result=MAV_RESULT_DENIED (2)',
])
def test_preserves_successful_and_failed_arming_attempts(tmp_path, event):
    retained = tmp_path / '7.log'
    contents = '2026-11-20 12:00:00,000 INFO     ' + event + '\n'
    retained.write_text(contents)
    (tmp_path / '8.log').write_text('Connecting to vehicle\nConnection failed\n')

    assert cleanup_unarmed_logs(tmp_path) == 1
    assert retained.read_text() == contents
    assert not (tmp_path / '8.log').exists()


def test_idle_and_connection_failure_messages_do_not_count_as_attempts(tmp_path):
    (tmp_path / '0.log').write_text(
        "vehicle = connect(uri, wait_ready=['gps_0', 'armed', 'mode'])\n"
        'core state is: AWAITING_ARM, n/a\n'
        'Vehicle armed 0\n'
        'Vehicle armed False\n'
        'PreArm: GPS fix required\n'
        'Disarming motors\n'
        'Removed 3 previous logs without an arming attempt\n'
    )
    (tmp_path / '1.log').touch()

    assert cleanup_unarmed_logs(tmp_path) == 2
    assert not list(tmp_path.iterdir())


def test_leaves_other_files_directories_and_symlinks_alone(tmp_path):
    notes = tmp_path / 'notes.txt'
    notes.write_text('Keep this')
    (tmp_path / 'archive.log').mkdir()
    (tmp_path / 'linked.log').symlink_to(notes)

    assert cleanup_unarmed_logs(tmp_path) == 0
    assert notes.read_text() == 'Keep this'
    assert (tmp_path / 'archive.log').is_dir()
    assert (tmp_path / 'linked.log').is_symlink()


@pytest.mark.parametrize('operation', ['open', 'unlink'])
def test_file_errors_preserve_log_and_allow_remaining_cleanup(tmp_path, monkeypatch, caplog, operation):
    retained = tmp_path / '0.log'
    retained.write_text('Connection failed\n')
    (tmp_path / '1.log').write_text('Connection failed\n')
    original = getattr(Path, operation)

    def fail_for_retained(path, *args, **kwargs):
        if path == retained:
            raise PermissionError('Access denied')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, fail_for_retained)
    assert cleanup_unarmed_logs(tmp_path) == 1
    assert retained.exists()
    assert 'retaining it: Access denied' in caplog.text


def test_undecodable_log_is_retained(tmp_path, caplog):
    retained = tmp_path / '0.log'
    retained.write_bytes(b'\xff\xfe')

    assert cleanup_unarmed_logs(tmp_path) == 0
    assert retained.read_bytes() == b'\xff\xfe'
    assert 'retaining it' in caplog.text


def test_numbering_uses_highest_retained_number(tmp_path):
    (tmp_path / '0.log').write_text('Vehicle armed 1\n')
    (tmp_path / '2.log').write_text('Waiting for arming...\n')
    (tmp_path / 'notes.txt').touch()
    (tmp_path / 'named.log').write_text('Vehicle armed 1\n')

    assert next_log_path(tmp_path) == tmp_path / '3.log'


def test_numbering_starts_at_zero_in_empty_directory(tmp_path):
    assert next_log_path(tmp_path) == tmp_path / '0.log'
