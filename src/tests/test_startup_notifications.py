"""Readiness waits and camera lifecycle checks without any camera or serial I/O."""

from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

import main as app
from camera import yolocam


@pytest.fixture
def camera():
    return SimpleNamespace(ready_event=threading.Event(), stopped_event=threading.Event(),
                           error=None, running=lambda: True)


@pytest.fixture
def startup(monkeypatch):
    events = []
    notifier = SimpleNamespace(service_ready=lambda: events.append('ready'),
                               start_rc_notifications=lambda: events.append('rc'))
    core = SimpleNamespace(notifier=notifier)
    monkeypatch.setattr(app, 'Notifier', lambda vehicle: notifier)
    def make_core(vehicle, camera, notifier):
        events.append('core')
        return core
    monkeypatch.setattr(app, 'Core', make_core)
    monkeypatch.setattr(app, 'notification_settings', replace(app.notification_settings, camera_ready_timeout=0.005))
    return SimpleNamespace(_trusted_fc_system_id=42, last_heartbeat=0), events, core


def test_ready_is_after_camera_output_and_core_initialization(camera, startup):
    vehicle, events, core = startup
    camera.ready_event.set()
    assert app.prepare_tracking_core(vehicle, camera, lambda: False) is core
    assert events == ['core', 'ready', 'rc']


def test_camera_timeout_never_announces_ready(camera, startup):
    vehicle, events, _ = startup
    with pytest.raises(TimeoutError):
        app.prepare_tracking_core(vehicle, camera, lambda: False)
    assert not events


def test_camera_failure_never_announces_ready(camera, startup):
    vehicle, events, _ = startup
    camera.error = RuntimeError('Camera unavailable')
    camera.stopped_event.set()
    with pytest.raises(RuntimeError, match='Camera stopped'):
        app.prepare_tracking_core(vehicle, camera, lambda: False)
    assert not events


def test_cancellation_never_announces_ready(camera, startup):
    vehicle, events, _ = startup
    camera.ready_event.set()
    assert app.prepare_tracking_core(vehicle, camera, lambda: True) is None
    assert not events


@pytest.mark.parametrize('identity,heartbeat', [(None, 0), (42, 1000)])
def test_disconnected_controller_never_announces_ready(camera, startup, identity, heartbeat):
    vehicle, events, _ = startup
    vehicle._trusted_fc_system_id, vehicle.last_heartbeat = identity, heartbeat
    camera.ready_event.set()
    with pytest.raises(RuntimeError, match='connection is not ready'):
        app.prepare_tracking_core(vehicle, camera, lambda: False)
    assert not events


def test_empty_detection_batch_is_ready_without_a_person(monkeypatch):
    monkeypatch.setattr(yolocam.YoloCamera, 'setup', lambda self: None)
    monkeypatch.setattr(yolocam, 'THREAD_STOP', False)
    camera = yolocam.YoloCamera()
    camera._callback([], None)
    assert camera.ready_event.is_set()
    assert camera.detections() == []


def test_camera_worker_failure_clears_readiness(monkeypatch):
    monkeypatch.setattr(yolocam.YoloCamera, 'setup', lambda self: setattr(self, '_pipeline', object()))
    def fail(*args):
        raise RuntimeError('Simulated camera failure')
    monkeypatch.setattr(yolocam, 'thread', fail)
    camera = yolocam.YoloCamera()
    camera.start()
    assert camera.stopped_event.wait(1)
    camera.stop()
    assert isinstance(camera.error, RuntimeError)
    assert not camera.ready_event.is_set()
    assert not camera.running()


def test_idle_packet_wait_can_be_cancelled(monkeypatch):
    monkeypatch.setattr(yolocam, 'THREAD_STOP', False)
    def try_get():
        yolocam.THREAD_STOP = True
        return None
    monkeypatch.setattr(yolocam.time, 'sleep', lambda duration: None)
    assert yolocam._get_packet(SimpleNamespace(tryGet=try_get)) is None


def test_ready_notification_error_does_not_prevent_core_start(camera, startup, monkeypatch):
    vehicle, events, core = startup
    camera.ready_event.set()
    def fail():
        raise OSError('Notification unavailable')
    monkeypatch.setattr(core.notifier, 'service_ready', fail)
    assert app.prepare_tracking_core(vehicle, camera, lambda: False) is core
    assert events == ['core', 'rc']


def test_rc_setup_error_does_not_prevent_core_start(camera, startup, monkeypatch):
    vehicle, events, core = startup
    camera.ready_event.set()

    def fail():
        raise OSError('RC telemetry unavailable')

    monkeypatch.setattr(core.notifier, 'start_rc_notifications', fail)
    assert app.prepare_tracking_core(vehicle, camera, lambda: False) is core
    assert events == ['core', 'ready']


@pytest.fixture
def main_runtime(tmp_path, monkeypatch):
    import sys
    # Restore pytest's streams after main's legacy stdout/stderr redirection.
    monkeypatch.setattr(sys, 'stdout', sys.stdout)
    monkeypatch.setattr(sys, 'stderr', sys.stderr)
    for name in ('core', 'camera', 'vehicle', 'videoWriter'):
        monkeypatch.setattr(app, name, None)
    monkeypatch.setattr(app, 'EXIT', False)
    monkeypatch.setattr(app.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(app.logging, 'basicConfig', lambda **kwargs: None)
    events = []
    camera = SimpleNamespace(start=lambda: events.append('camera-start'),
                             stop=lambda: events.append('camera-stop'))
    vehicle = SimpleNamespace(close=lambda: events.append('vehicle-close'))
    monkeypatch.setattr(app, 'YoloCamera', lambda callback: camera)
    monkeypatch.setattr(app, 'connect', lambda *args, **kwargs: vehicle)
    monkeypatch.setattr(app.time, 'sleep', lambda duration: setattr(app, 'EXIT', True))
    args = SimpleNamespace(log_path=str(tmp_path), video=False, video_path=str(tmp_path),
                           killswitch_path=str(tmp_path / 'killswitch'), uri='/dev/fake')
    return args, events, camera, vehicle


def test_main_closes_camera_and_link_on_readiness_failure(main_runtime, monkeypatch):
    args, events, _, _ = main_runtime
    def fail(*args):
        raise TimeoutError('No first frame')
    monkeypatch.setattr(app, 'prepare_tracking_core', fail)
    with pytest.raises(TimeoutError):
        app.main(args)
    assert events == ['camera-start', 'camera-stop', 'vehicle-close']


def test_killswitch_skips_connection_and_readiness(main_runtime, monkeypatch):
    from pathlib import Path
    args, events, _, _ = main_runtime
    Path(args.killswitch_path).touch()
    def forbidden(*args, **kwargs):
        pytest.fail('Killswitch must prevent connecting and announcing readiness')
    monkeypatch.setattr(app, 'connect', forbidden)
    monkeypatch.setattr(app, 'prepare_tracking_core', forbidden)
    app.main(args)
    assert events == ['camera-start', 'camera-stop']


def test_main_prepares_notifications_before_starting_control_thread(main_runtime, monkeypatch):
    args, events, _, vehicle = main_runtime
    core = SimpleNamespace(state='AWAITING_ARM', activeRule='n/a',
                           stop=lambda: events.append('core-stop'))
    def prepare(*args):
        events.append('ready')
        return core
    monkeypatch.setattr(app, 'prepare_tracking_core', prepare)
    monkeypatch.setattr(app.threading, 'Thread', lambda **kwargs: SimpleNamespace(
        start=lambda: events.append('core-start'), join=lambda timeout: events.append('core-join'),
    ))
    app.main(args)
    assert events.index('ready') < events.index('core-start')
    assert 'core-stop' in events and 'camera-stop' in events


def test_main_cleans_logs_once_before_opening_log_and_connecting(main_runtime, monkeypatch):
    from pathlib import Path
    args, events, _, _ = main_runtime
    directory = Path(args.log_path)
    failed = directory / '0.log'
    failed.write_text('Connection failed\n')
    retained = directory / '2.log'
    retained.write_text('Vehicle armed 1\n')
    cleanup_calls = []
    cleanup = app.cleanup_unarmed_logs

    def record_cleanup(path):
        cleanup_calls.append(path)
        assert not events
        assert not (directory / '3.log').exists()
        return cleanup(path)

    def fail_connection(*args, **kwargs):
        assert not failed.exists()
        assert (directory / '3.log').exists()
        raise OSError('Flight controller unavailable')

    monkeypatch.setattr(app, 'cleanup_unarmed_logs', record_cleanup)
    monkeypatch.setattr(app, 'connect', fail_connection)
    with pytest.raises(OSError, match='Flight controller unavailable'):
        app.main(args)

    assert cleanup_calls == [args.log_path]
    assert retained.read_text() == 'Vehicle armed 1\n'
    assert (directory / '3.log').exists()
    assert events == ['camera-start', 'camera-stop']


def test_control_thread_failure_records_traceback(monkeypatch, caplog):
    monkeypatch.setattr(app, 'EXIT', False)

    def fail():
        raise RuntimeError('Control loop failure')

    with pytest.raises(RuntimeError, match='Control loop failure'):
        app.core_thread(SimpleNamespace(run=fail))
    assert 'Control thread failed' in caplog.text
    assert any(record.exc_info for record in caplog.records)
