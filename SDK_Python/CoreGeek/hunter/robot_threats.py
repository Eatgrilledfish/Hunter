"""Observed robot fields first; missing fields use confirmed taskbook 4.7.2."""
from dataclasses import replace

POWER = {'smallRobot': 5, 'middleRobot': 10, 'largeRobot': 20, 'bossRobot': 40}


def resolved(robot):
    return replace(robot,
        attack_range=robot.attack_range if robot.attack_range is not None else (3 if robot.kind in POWER else None),
        attack_power=robot.attack_power if robot.attack_power is not None else POWER.get(robot.kind))


def active(world):
    return [resolved(r) for r in world.robots.values() if r.alive and r.abnormal != 'dizzy']
