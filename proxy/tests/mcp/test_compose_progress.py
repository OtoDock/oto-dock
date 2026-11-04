"""The compose run's record (services/mcp/compose_progress.py): the phase and
the plain sentence the MCP Servers page shows while an update pulls."""

from __future__ import annotations

import pytest

from services.mcp import compose_progress as cp


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _no_records():
    cp._records.clear()
    yield
    cp._records.clear()


def _tracker(name: str = "camoufox") -> tuple[cp.Tracker, _Clock]:
    clock = _Clock()
    return cp.Tracker(name, clock=clock), clock


def test_compose_v5_lines_read_as_download_unpack_and_their_sizes():
    t, clock = _tracker()
    t.feed("Image ghcr.io/otodock/camoufox:0.0.77 Pulling")
    assert cp.progress_of("camoufox") == {"phase": "pulling", "detail": "Looking for the new image."}
    t.feed("1d7a63b094ee Pulling fs layer 0B")
    t.feed("8be8a68a3e49 Pulling fs layer 0B")
    t.feed("1d7a63b094ee Downloading 700MB")
    assert cp.progress_of("camoufox")["phase"] == "downloading"
    clock.t += 1.5
    t.feed("8be8a68a3e49 Downloading 512.3MB")
    assert cp.progress_of("camoufox") == {
        "phase": "downloading", "detail": "Downloading the new image, 1.2 GB so far."}
    t.feed("1d7a63b094ee Download complete 0B")
    t.feed("8be8a68a3e49 Download complete 0B")
    # A phase change is written at once, whatever the throttle.
    assert cp.progress_of("camoufox") == {"phase": "extracting", "detail": "Unpacking the image."}
    t.feed("1d7a63b094ee Extracting 1B")
    t.feed("1d7a63b094ee Pull complete 0B")
    t.feed("8be8a68a3e49 Pull complete 0B")
    t.feed("Image ghcr.io/otodock/camoufox:0.0.77 Pulled")
    assert cp.progress_of("camoufox")["phase"] == "extracting"
    t.close()
    assert cp.progress_of("camoufox") is None


def test_the_older_bar_format_reads_the_bytes_so_far():
    t, _ = _tracker()
    t.feed(" 3f4ca61aafcd Downloading [===>   ]  55MB/2.03GB")
    assert cp.progress_of("camoufox")["detail"] == "Downloading the new image, 55 MB so far."


def test_a_layer_still_downloading_keeps_the_phase_while_another_unpacks():
    t, clock = _tracker()
    t.feed("aaaaaaaaaaaa Downloading 3MB")
    t.feed("aaaaaaaaaaaa Download complete 0B")
    t.feed("bbbbbbbbbbbb Downloading 1MB")
    t.feed("aaaaaaaaaaaa Extracting 1B")
    assert cp.progress_of("camoufox")["phase"] == "downloading"
    t.feed("bbbbbbbbbbbb Download complete 0B")
    assert cp.progress_of("camoufox")["phase"] == "extracting"


def test_a_byte_count_is_written_at_most_once_a_second():
    t, clock = _tracker()
    t.feed("aaaaaaaaaaaa Downloading 1MB")
    t.feed("aaaaaaaaaaaa Downloading 5MB")
    assert cp.progress_of("camoufox")["detail"].endswith("1 MB so far.")
    clock.t += 1.0
    t.feed("aaaaaaaaaaaa Downloading 6MB")
    assert cp.progress_of("camoufox")["detail"].endswith("6 MB so far.")


def test_an_up_reads_as_starting_and_a_build_as_building():
    t, _ = _tracker("file-tools")
    t.feed("Image otodock-x-mcp-file-tools Building")
    assert cp.progress_of("file-tools")["detail"] == "Building the image."
    t.feed("#7 [3/9] RUN pip install -r requirements.txt")
    assert cp.progress_of("file-tools")["phase"] == "building"
    t.feed("Container otodock-x-mcp-file-tools  Recreate")
    assert cp.progress_of("file-tools") == {"phase": "starting", "detail": "Starting the container."}


def test_lines_that_name_nothing_write_nothing():
    t, _ = _tracker()
    t.feed("some unrelated compose chatter")
    t.feed("")
    assert cp.progress_of("camoufox") is None


def test_a_second_run_on_the_same_mcp_keeps_its_record_when_the_first_ends():
    first, _ = _tracker()
    second, _ = _tracker()
    first.feed("Image x Pulling")
    second.feed("Container otodock-x-mcp-camoufox  Recreate")
    first.close()
    assert cp.progress_of("camoufox")["phase"] == "starting"
    second.close()
    assert cp.progress_of("camoufox") is None


@pytest.mark.parametrize("n, text", [(999, "1 kB"), (12_400_000, "12 MB"), (1_249_000_000, "1.2 GB")])
def test_sizes_read_the_way_docker_counts_them(n, text):
    assert cp.human_bytes(n) == text
