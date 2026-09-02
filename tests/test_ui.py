"""Progress plumbing: the task churn and thread-safety that HLS downloads expose."""

from __future__ import annotations

import threading

from rich.console import Console
from rich.progress import Progress

from media.ui import ItemView, human_duration, human_size


def _view() -> ItemView:
    """An ItemView backed by a real rich Progress, writing nowhere."""
    console = Console(file=open("/dev/null", "w"), force_terminal=False)
    progress = Progress(console=console)
    task_id = progress.add_task("Fetching info…", total=None, detail="")
    return ItemView(progress=progress, task_id=task_id, console=console)


def test_a_drifting_total_keeps_the_same_task():
    """HLS estimates creep up per fragment; that must not recreate the bar."""
    view = _view()
    view.set_bytes(10, 1_000_000, "Downloading video…")
    first = view.task_id

    for total in range(1_000_100, 1_010_000, 700):  # the estimate wandering upward
        view.set_bytes(total // 2, total, "Downloading video…")

    assert view.task_id == first


def test_a_new_label_starts_a_fresh_task():
    """Video then audio are two files and deserve two bars."""
    view = _view()
    view.set_bytes(10, 5_000, "Downloading video…")
    during_video = view.task_id

    view.set_bytes(10, 900, "Downloading audio…")

    assert view.task_id != during_video


def test_an_unknown_total_does_not_wipe_the_known_one():
    view = _view()
    view.set_bytes(10, 4_000, "Downloading video…")
    task = view.progress.tasks[-1]

    view.set_bytes(20, 0, "Downloading video…")  # a callback with no estimate

    assert task.total == 4_000


def test_concurrent_updates_do_not_race():
    """yt-dlp drives progress hooks from its fragment workers, four at a time.

    Before the lock, one thread could remove a task id another was about to
    update, and rich raised a bare KeyError that surfaced to the user as a
    number with no message.
    """
    view = _view()
    errors: list[BaseException] = []
    start = threading.Barrier(4)

    def worker(index: int) -> None:
        start.wait()
        try:
            for step in range(300):
                total = 1_000_000 + step * 977 + index
                label = "Downloading video…" if step % 40 else "Downloading audio…"
                view.set_bytes(step * 100, total, label)
        except BaseException as exc:  # noqa: BLE001 - the point is to catch anything
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"{type(errors[0]).__name__}: {errors[0]}"


def test_stage_and_fraction_survive_concurrent_downloads():
    """The pipeline calls stage() while hook threads are still calling set_bytes."""
    view = _view()
    errors: list[BaseException] = []
    stop = threading.Event()

    def hammer() -> None:
        try:
            step = 0
            while not stop.is_set():
                view.set_bytes(step, 1_000 + step, "Downloading video…")
                step += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=hammer)
    thread.start()
    try:
        for _ in range(200):
            view.stage("merge")
            view.set_fraction(0.5, key="convert")
    finally:
        stop.set()
        thread.join()

    assert not errors, f"{type(errors[0]).__name__}: {errors[0]}"


def test_inactive_view_is_a_no_op():
    """--quiet and non-TTY runs get a view with no progress at all."""
    view = ItemView(progress=None, task_id=None, console=Console(), quiet=True)

    view.set_bytes(1, 2, "Downloading video…")
    view.stage("merge")
    view.set_fraction(0.5)

    assert not view.active


def test_human_helpers_round_trip():
    assert human_size(0) == "0 B"
    assert human_duration(0) == "0:00"
