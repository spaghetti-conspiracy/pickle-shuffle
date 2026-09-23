"""試合時計。

締切の時刻ではなく経過秒を持つので、試合の途中で持ち時間を変えても
その場で残り時間が変わる。表示画面はこの経過を起点に自分で数える。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.errors import ConflictError
from app.models import TimerState, utcnow
from app.services import rounds as rounds_service
from tests.test_rounds_service import make_session


def _adopted(db):
    session = make_session(db)
    round_ = rounds_service.generate(db, session)
    return session, rounds_service.adopt(db, round_)


def test_starting_a_match_starts_the_clock(db):
    """開始を押した時点で動き出す。別に押す操作を増やさない。"""
    _, round_ = _adopted(db)
    assert round_.timer_state is TimerState.RUNNING
    assert rounds_service.elapsed_seconds(round_) >= 0


def test_elapsed_grows_while_running(db):
    _, round_ = _adopted(db)
    round_.timer_started_at = utcnow() - timedelta(seconds=90)
    assert rounds_service.elapsed_seconds(round_) == pytest.approx(90, abs=2)


def test_pausing_freezes_the_clock(db):
    """一時停止の間は増えない。"""
    _, round_ = _adopted(db)
    round_.timer_started_at = utcnow() - timedelta(seconds=60)
    rounds_service.pause_timer(db, round_)
    assert round_.timer_state is TimerState.PAUSED
    frozen = rounds_service.elapsed_seconds(round_)
    assert frozen == pytest.approx(60, abs=2)
    assert rounds_service.elapsed_seconds(round_) == frozen


def test_resuming_continues_from_where_it_stopped(db):
    """再開しても、止めていた間は加算されない。"""
    _, round_ = _adopted(db)
    round_.timer_started_at = utcnow() - timedelta(seconds=60)
    rounds_service.pause_timer(db, round_)
    rounds_service.resume_timer(db, round_)
    assert round_.timer_state is TimerState.RUNNING
    assert rounds_service.elapsed_seconds(round_) == pytest.approx(60, abs=2)


def test_stopping_keeps_the_elapsed_but_ends_the_clock(db):
    """中断は一時停止と違い、再開しない。"""
    _, round_ = _adopted(db)
    round_.timer_started_at = utcnow() - timedelta(seconds=30)
    rounds_service.stop_timer(db, round_)
    assert round_.timer_state is TimerState.STOPPED
    with pytest.raises(ConflictError):
        rounds_service.resume_timer(db, round_)


def test_pausing_a_stopped_clock_is_rejected(db):
    _, round_ = _adopted(db)
    rounds_service.stop_timer(db, round_)
    with pytest.raises(ConflictError):
        rounds_service.pause_timer(db, round_)


# ---------------------------------------------------------------------------
# API から見た姿
# ---------------------------------------------------------------------------


def test_the_timer_is_not_part_of_the_revision(client):
    """経過秒で revision が変わってはいけない。

    変わると2秒ごとに全体表示画面が描き直され、読み上げの最中にちらつく。
    """
    from tests.test_api import add_members, create_session

    session = create_session(client)
    add_members(client, session["token"], 13)
    client.post(f"/api/sessions/{session['token']}/rounds/generate")
    current = client.get(f"/api/sessions/{session['token']}/current").json()
    round_id = current["round_id"]
    client.post(f"/api/rounds/{round_id}/adopt")

    first = client.get(f"/api/sessions/{session['token']}/current").json()
    second = client.get(f"/api/sessions/{session['token']}/current").json()
    assert first["revision"] == second["revision"]
    assert first["timer"]["state"] == "running"


def test_changing_the_limit_mid_match_changes_the_remaining_time(client):
    """試合中に持ち時間を変えたら、その場で反映される。

    締切の時刻を持っていると、設定を変えても止まった時計のままになる。
    経過を持っているので、残り時間だけが変わる。
    """
    from tests.test_api import add_members, create_session

    session = create_session(client)
    add_members(client, session["token"], 13)
    current = client.post(
        f"/api/sessions/{session['token']}/rounds/generate"
    ).json()
    client.post(f"/api/rounds/{current['round_id']}/adopt")

    before = client.get(f"/api/sessions/{session['token']}/current").json()["timer"]
    assert before["limit_seconds"] == 7 * 60, "既定は7分"

    client.patch(f"/api/sessions/{session['token']}", json={"timer_minutes": 3})
    after = client.get(f"/api/sessions/{session['token']}/current").json()["timer"]
    assert after["limit_seconds"] == 3 * 60
    assert after["elapsed_seconds"] >= before["elapsed_seconds"], "経過は巻き戻らない"


def test_unlimited_has_no_limit(client):
    from tests.test_api import create_session

    session = create_session(client)
    client.patch(f"/api/sessions/{session['token']}", json={"unlimited": True})
    timer = client.get(f"/api/sessions/{session['token']}/current").json()["timer"]
    assert timer["limit_seconds"] is None
    assert timer["timed_out"] is False


def test_the_limit_is_between_three_and_fifteen_minutes(client):
    from tests.test_api import create_session

    session = create_session(client)
    for minutes in (2, 16):
        response = client.patch(
            f"/api/sessions/{session['token']}", json={"timer_minutes": minutes}
        )
        assert response.status_code == 422, f"{minutes}分が通ってしまう"
    for minutes in (3, 7, 15):
        assert (
            client.patch(
                f"/api/sessions/{session['token']}", json={"timer_minutes": minutes}
            ).status_code
            == 200
        )


def test_pause_and_resume_through_the_api(client):
    from tests.test_api import add_members, create_session

    session = create_session(client)
    add_members(client, session["token"], 13)
    current = client.post(
        f"/api/sessions/{session['token']}/rounds/generate"
    ).json()
    round_id = current["round_id"]
    client.post(f"/api/rounds/{round_id}/adopt")

    paused = client.post(f"/api/rounds/{round_id}/timer/pause").json()
    assert paused["timer"]["state"] == "paused"
    resumed = client.post(f"/api/rounds/{round_id}/timer/resume").json()
    assert resumed["timer"]["state"] == "running"
    stopped = client.post(f"/api/rounds/{round_id}/timer/stop").json()
    assert stopped["timer"]["state"] == "stopped"


def test_an_unknown_timer_action_is_refused(client):
    from tests.test_api import add_members, create_session

    session = create_session(client)
    add_members(client, session["token"], 13)
    current = client.post(
        f"/api/sessions/{session['token']}/rounds/generate"
    ).json()
    client.post(f"/api/rounds/{current['round_id']}/adopt")
    response = client.post(f"/api/rounds/{current['round_id']}/timer/rewind")
    assert response.status_code == 422
