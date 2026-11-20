"""The selected rule is reported without allowing notifier errors to stop control."""

from types import SimpleNamespace

from src.dronekit_compat import apply_dronekit_compatibility
apply_dronekit_compatibility()
import core as module
from core import Core, ExecutionState


def fake_vehicle():
    return SimpleNamespace(add_attribute_listener=lambda *args: None, armed=False)


def test_core_instances_do_not_share_rules():
    a, b = Core(fake_vehicle(), object()), Core(fake_vehicle(), object())
    assert a.rules is not b.rules
    assert [r.name() for r in a.rules] == ['backoff', 'follow', 'search', 'none']
    assert len(b.rules) == 4


def test_only_highest_priority_active_rule_is_reported():
    reported = []
    core = Core(fake_vehicle(), object(), SimpleNamespace(update_rule=reported.append))
    def rule(name, command):
        return SimpleNamespace(update=lambda: None, isActive=lambda: True,
                               getState=lambda: command, name=lambda: name)
    core.rules = [rule('backoff', ((-1, 0), 0)), rule('follow', ((1, 0), 10))]
    assert core._rule_command() == ((-1, 0), 0)
    assert core.activeRule == 'backoff'
    assert reported == ['backoff']


def test_notifier_exception_does_not_prevent_movement_command(monkeypatch, caplog):
    def fail(rule):
        raise OSError('Simulated notifier failure')
    core = Core(fake_vehicle(), object(), SimpleNamespace(update_rule=fail))
    core.rules = [SimpleNamespace(update=lambda: None, isActive=lambda: True,
                                 getState=lambda: ((1, 2), 3), name=lambda: 'follow')]
    core.state = ExecutionState.Running
    commands = []
    monkeypatch.setattr(module, 'setPositionTarget', lambda *args: commands.append(args[1:]))
    monkeypatch.setattr(module.time, 'sleep', lambda _: setattr(core, 'state', ExecutionState.Stop))
    core.run()
    assert commands == [((1, 2), 3)]
    assert 'movement control continues' in caplog.text
