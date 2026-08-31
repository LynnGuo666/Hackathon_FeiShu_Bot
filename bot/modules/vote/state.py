"""投票通道状态。"""
from __future__ import annotations


_vote_open = True


def is_vote_open() -> bool:
    return _vote_open


def set_vote_open(opened: bool) -> None:
    global _vote_open
    _vote_open = opened

