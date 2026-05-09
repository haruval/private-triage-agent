#!/usr/bin/env python3
"""Download a small slice of the Enron email corpus for development.

Streams the canonical CMU tarball (~423 MB), samples ~200 messages with
reservoir sampling, and writes them to data/dev_corpus.mbox. Emails are
written as-is — raw, messy, RFC-5322 real-world data is the point.

Usage:
    python scripts/fetch_enron.py
    python scripts/fetch_enron.py --count 500 --seed 7

The tarball is cached under data/raw/ (gitignored) so re-runs with a
different seed don't re-download. Delete data/raw/ to reclaim disk.
"""

import argparse
import mailbox
import random
import sys
import tarfile
import urllib.request
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

ENRON_URL = "https://www.cs.cmu.edu/~./enron/enron_mail_20150507.tar.gz"
DEFAULT_TARBALL = Path("data/raw/enron_mail_20150507.tar.gz")
DEFAULT_OUTPUT = Path("data/dev_corpus.mbox")
DEFAULT_COUNT = 200
DEFAULT_SEED = 42

console = Console()


def download_tarball(url: str, dest: Path) -> None:
    """Stream-download to a .partial file, then rename atomically.

    A partial download (Ctrl-C, network error) leaves no half-written file
    at `dest`, so the next run will re-download cleanly.
    """
    if dest.exists():
        console.print(f"[dim]Tarball already cached at {dest} — skipping download.[/dim]")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")

    console.print(f"Downloading [bold]{url}[/bold]")
    console.print("[dim]One-time ~423 MB download. Cached afterwards under data/raw/.[/dim]")

    try:
        with urllib.request.urlopen(url) as response:
            total = int(response.headers.get("Content-Length", 0))
            with Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Downloading", total=total)
                with tmp.open("wb") as out:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        progress.update(task, advance=len(chunk))
        tmp.rename(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def sample_emails(tarball: Path, count: int, seed: int) -> list[bytes]:
    """Single-pass reservoir sampling over the tarball.

    Iterates the tar archive once, decompressing as it goes. Keeps a
    bounded reservoir of `count` messages with uniform sampling probability
    (Algorithm R). Memory is bounded; the gzip stream is decompressed only
    once.
    """
    rng = random.Random(seed)
    reservoir: list[bytes] = []
    seen = 0

    with console.status("[bold green]Streaming tarball...") as status:
        with tarfile.open(tarball, "r:gz") as tar:
            for member in tar:
                if not member.isfile() or not member.name.startswith("maildir/"):
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                content = f.read()
                seen += 1

                if len(reservoir) < count:
                    reservoir.append(content)
                else:
                    j = rng.randrange(seen)
                    if j < count:
                        reservoir[j] = content

                if seen % 5000 == 0:
                    status.update(
                        f"[bold green]Streamed {seen:,} messages — "
                        f"reservoir holds {len(reservoir)}"
                    )

    if not reservoir:
        console.print("[red]No emails found in tarball — is the path correct?[/red]")
        sys.exit(1)

    console.print(f"Sampled {len(reservoir)} from {seen:,} total messages.")
    return reservoir


def write_mbox(messages: list[bytes], output: Path) -> int:
    """Write raw bytes into an mbox file. Returns number of messages written."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    written = 0
    box = mailbox.mbox(str(output))
    box.lock()
    try:
        for raw in messages:
            try:
                box.add(mailbox.mboxMessage(raw))
                written += 1
            except Exception as exc:
                # Real-world data is messy — log and skip.
                console.print(f"[yellow]Skipping malformed message: {exc}[/yellow]")
        box.flush()
    finally:
        box.unlock()
        box.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"Number of messages to sample (default: {DEFAULT_COUNT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output mbox path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--tarball",
        type=Path,
        default=DEFAULT_TARBALL,
        help="Where to cache the downloaded tarball",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Random seed for reproducible sampling (default: {DEFAULT_SEED})",
    )
    args = parser.parse_args()

    download_tarball(ENRON_URL, args.tarball)
    messages = sample_emails(args.tarball, args.count, args.seed)
    written = write_mbox(messages, args.output)

    console.print(
        f"[green]Wrote[/green] [bold]{written}[/bold] messages to "
        f"[bold]{args.output}[/bold]"
    )
    console.print(
        f"[dim]Source tarball cached at {args.tarball} — "
        f"delete to reclaim disk.[/dim]"
    )


if __name__ == "__main__":
    main()
