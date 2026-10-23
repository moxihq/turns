from moxi_turns.ledger import TurnLedger


def test_the_ledger():
    led = TurnLedger("s-1")
    r0 = led.read()
    assert (r0.epoch, r0.turn_open, r0.session_id) == (0, False, "s-1")
    prev, turn = led.started("t1", "d1")
    assert prev is None and led.read().turn_open and led.read().epoch == 1
    assert led.steered_into("d2") is turn and turn.steered == ["d2"]
    assert led.ended("answer") is turn and turn.outcome == "answer"
    assert led.ended("answer") is None and led.steered_into("d3") is None
    v = led.read().version
    assert not led.new_session("s-1") and led.read().version == v
    assert led.new_session("s-2")
    r = led.read()
    assert (r.session_id, r.epoch, led.earlier) == ("s-2", 0, ["s-1"]) and r.version > v
    led.started("t2", None)
    prev, _ = led.started("t3", None)
    assert prev.turn_id == "t2" and prev.outcome == "error"


def test_an_ending_turn_stays_open_and_takes_no_steer():
    # The ending form: Codex was told the turn may end. It is still open (a start
    # is still refused busy, held mail still waits) but no steer goes into it.
    led = TurnLedger("s-1")
    _, turn = led.started("t1", "d1")
    assert led.steered_into("d2") is turn
    assert led.ending() is turn and turn.ending
    reading = led.read()
    assert reading.turn_open and reading.turn_id == "t1"
    assert led.steered_into("d3") is None and turn.steered == ["d2"]
    assert led.ended("answer") is turn
    assert led.ending() is None
    _, second = led.started("t2", None)
    assert not second.ending and led.steered_into("d4") is second
