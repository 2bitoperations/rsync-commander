# rsync-commander

A dual-panel (Norton Commander style) file manager for macOS and Linux. It syncs
big files (astrophotography subs, stacks) between local disks and NAS boxes over
ssh. Every block is checksummed, only out-of-date blocks are sent, and the
transfer view shows every block of every file.

```sh
uv run rsync-commander                                   # GUI (Qt)
uv run rsync-commander tui ~/astro nas:/volume1/astro    # same thing in a terminal
uv run rsync-commander batch examples/observatory.toml   # unattended profile run
uv run rsync-commander copy ~/astro/M42 nas:/volume1/astro   # one-shot copy
uv run rsync-commander copy nas:/volume1/astro/M42 ~/astro -j 6 -v
uv run rsync-commander copy -z gzip --python python2 ~/astro/M42 user@host:/data
uv run rsync-commander probe nas              # remote python + codecs
uv run rsync-commander install-zstd nas       # opt-in zstd helper (see below)
uv run rsync-commander install-app            # Dock/Spotlight launcher with name + icon
```

## Using it

- Each panel picks **Local**, any `Host` from `~/.ssh/config`, or a typed `user@host`.
  It remembers the last directory per endpoint.
- **Drag and drop** items between panels to copy them, or drop onto a folder row to
  copy into that folder. Files dragged in from Finder or other file managers work too.
  **F5** copies the selection to the other panel.
- F6 rename, F7 mkdir, F8/Delete delete, Ctrl+R refresh, Ctrl+U swap panels,
  Tab switch panel, Enter opens a directory, Backspace goes up, Space/Insert toggles the selection.
- The copy dialog lets you edit the target and set the options:
  - **Verify**: on (default) compares existing files block by block with SHA-1.
    Off skips files whose size and mtime already match.
  - **Streams**: the number of parallel ssh connections.
  - **Block size** and **wire compression**.
- The transfer pane shows the queue (jobs run one at a time) and, for the selected job:
  - totals, checksum progress, speed (data and on-the-wire), elapsed time and ETA;
  - what each stream is doing, with a progress bar for the block it is on;
  - a per-file table;
  - a **block map** of the active file (or any file you click). Each block shows
    unchecked, checksumming, identical, pending, transferring (filling up live),
    sent or failed. Hover a block to see its byte range.

**SSH auth.** The `copy` and `batch` commands authenticate at most once per host
(`--ssh-mux auto`). If key auth works there are no prompts, and every stream gets its
own TCP connection. If it doesn't, you're asked once (password or key passphrase)
for a single master connection that all streams share. That works, but it is
limited to one TCP stream's throughput, so `ssh-copy-id` is worth it on slow links.
The GUI and TUI need working keys for now.

Requirements: ssh access to the host and **Python 2.7 or 3.x** on the remote. The app uses the
first of `python3`, `python`, `python2` on the remote PATH, or the one given with `--python`.

## Compression

Pick `zstd` (default), `gzip` or `none`, with an optional level (0 = default:
zstd 3, gzip 1). Blocks are compressed only on network legs, and only when a block
shrinks by more than 8%, so noise or already-compressed data goes raw. The transfer
view shows the codec in use and what share of the data actually went over the wire.

Ratios on real 4 MiB blocks of NGC 598 subs (89 MiB, 16-bit). Speeds are per core on an M-series Mac:

| codec | L | R | B | Ha | speed |
|---|---|---|---|---|---|
| gzip -1 | 49% | 47% | 46% | 38% | ~65 MiB/s |
| gzip -6 | 45% | 43% | 42% | 32% | ~5 MiB/s |
| zstd -1 | 48% | 44% | 43% | 33% | ~400 MiB/s |
| **zstd -3** | 46% | 43% | 42% | 32% | ~190 MiB/s |
| zstd -9 | 43% | 40% | 39% | 31% | ~42 MiB/s |
| zstd -19 | 40% | 37% | 36% | 27% | ~3 MiB/s |

So compression roughly doubles throughput on a slow link. zstd 3 matches gzip 6's
ratio at about 40× the speed. The sender does the compressing: the NAS's CPU for
pulls, this machine for pushes. Above level ~9 a 2-core NAS becomes the bottleneck.

The agent finds zstd in Python 3.14's `compression.zstd`, in the `zstandard`
package, or in a `zstd` binary on PATH. If either end lacks it, the job falls back
to gzip and says so in Notes. `install-zstd HOST` puts a matching
`zstandard` wheel in `HOST:~/.rcommander/lib`. It is downloaded here, since NAS boxes have no pip.
Remove it with `rm -rf ~/.rcommander`.

## Terminal UI

`rsync-commander tui [LEFT] [RIGHT]` is the same commander in the terminal (Textual),
so it also works over ssh on a headless box. It uses the same keys as the GUI: Tab,
Enter, Backspace, Space/Insert to mark, F2 host, Ctrl+L go to path, F5 copy, F6 rename,
F7 mkdir, F8 delete, F9 transfers, F10 quit. The mouse works too, and the footer is clickable.

Progress uses btop-style `■` meters, with the gradient laid across the full width,
and a braille speed graph. **F9** opens the transfer detail: totals, checksum meter,
a meter per stream for the block it is on, per-file meters, and a block map of the
active file (`f` follows the active file, `[` `]` switch jobs, `p` pauses, `c` cancels).

## Batch profiles

`rsync-commander batch PROFILE.toml` runs a sync described in TOML, for cron, systemd
timers or a quick `ssh box rsync-commander batch ...`.
`--detect` shows what the globs resolve to. `--dry-run [-v]` lists what would be synced.
`-o key=value` overrides `[options]`. Progress goes to stdout as a live line on
a terminal, or as timestamped log lines otherwise (`--interval`). Exit code: 0 ok,
1 transfer errors, 2 profile/config errors.

A profile has:

- `dest`: `host:/path` or a local path.
- `[options]`: workers, block_size_mib, checksum, compression, level.
- `[[source]]`: glob paths (first match wins), `to` (a path under dest),
  rsync-style filters (`+ pattern` / `- pattern`, first match wins, excluded
  dirs are pruned), and `follow_symlinks`.
- `[[hook]]`: shell commands to run before or after.
- `[[archive]]`: pack paths into a timestamped `.tar.xz` that is synced too.

String fields can use `{hostname}`, `{date}`, `{timestamp}`, `{user}`, `{home}` and
`{stage}`. The full reference is the docstring of `src/rcommander/batch.py`.

`examples/observatory.toml` replaces the old `observatory-backup` script. It
produces the same destination layout:

- light frames, minus focus/pointing/slew/guide runs;
- guider darks only;
- the TheSkyX logs;
- a settings snapshot tarball with the config, udev rules and system reports.

Put your real destination in a copy named `*.local.toml`. Files with that name are gitignored.

**Headless boxes** don't need Qt:

```sh
uv run --no-group gui rsync-commander batch observatory.local.toml
uv run --no-group gui rsync-commander tui
```

## How it works

Instead of driving `rsync`, the app runs its own small agent
(`src/rcommander/agent.py`, stdlib only, Python 3.8 compatible). On connect it
**streams the agent over ssh into the remote `python3 -c`**, so nothing is
installed on the NAS. The same code runs in-process for local paths.

For each file:

1. **prepare**: on the destination, find a basis: a `.<name>.rcpart` left by an
   interrupted run (resume), the existing file, or nothing.
2. **hash**: if there is a basis, both sides SHA-1 it per block in parallel,
   16 blocks per request. Identical blocks are skipped.
3. **clone**: if anything differs, the existing file is copied to the
   `.rcpart`. This is a CoW reflink on btrfs (Synology) and APFS (macOS), so it is instant.
4. **block**: each needed block is read, hashed at the source, sent (optionally
   zlib'd), and re-hashed and verified at the destination before it is written.
5. **finalize**: truncate, fsync, set mtime and mode, then rename into place.
   Until this step the final file is never half-written.

N worker threads each own one ssh connection per remote endpoint. That gives
N independent TCP streams, which matters on high-latency links (see below).
Dropped connections are retried with backoff on a fresh session, and work
already done is kept.

Not copied: `@eaDir` (Synology thumbnails), `#recycle`, `#snapshot`, `.DS_Store`,
and symlinks (listed in the job log).

## Target notes

**Synology DSM 7.4 (Celeron J4025, btrfs)**

| | |
|---|---|
| Python | 3.8 at `/usr/bin/python3`, no pip, no zstd module (`install-zstd` fixes that) |
| rsync | 3.1.2 (protocol 31). Works over ssh. |
| SFTP | can be **disabled** ("subsystem request failed"). Modern `scp` then fails; `scp -O` works. |
| hashing | sha1 1.38 GB/s (SHA-NI), so SHA-1 is the block hash |
| `cp --reflink` | supported, so cloning a basis file for a delta update is instant |

On a ~90 ms RTT WAN link, one stream managed ~0.8 MB/s up and ~0.3 MB/s down. Four
streams gave ~2.7× that. On 25.7 MiB, `rsync -a` pushed at ~0.6 MB/s and rsync-commander
with 4 streams at 2.8 MiB/s. A re-sync after corrupting 4 bytes on the NAS sent one
3.6 MiB block and verified the other 22.1 MiB as identical.

**Shared seedbox host (Python 2.7)**: `python` is 2.7.18, plus a `zstd` binary. The agent
runs on 2.7 (`--python python2`) and compresses through the zstd binary. Nothing is installed.

Put per-host notes in `HOSTS.local.md`. It is gitignored.

## Known limits / next steps

- **Real installer**: a self-contained, signed .app/.dmg for macOS and an AppImage or
  package for Linux, so it doesn't depend on a uv checkout. For now
  `rsync-commander install-app` makes a launcher that runs the checkout.
- GUI and TUI need working ssh keys. Only `copy` and `batch` do the
  ask-once password flow.

- Upload progress within a block counts bytes handed to ssh. That runs ahead of the
  real network by up to ssh's buffer (a few MB); download progress is exact.
- Diffing uses fixed-size blocks (no rolling hash). That suits FITS/RAW files,
  which get replaced or appended, not edited in the middle.
- NAS→NAS copies are relayed through this machine. Server-side copy on the
  same host, and direct box-to-box transfer, are not done yet.
- No move (copy + delete source) or mirror (delete extraneous) mode yet.
- Hosts without any Python would need an rsync/`scp -O` fallback engine (not written).
- With Python 2.7 remotes, filenames that aren't valid UTF-8 are listed with
  replacement characters and can't be transferred.
- Many tiny files cost ~3 round trips each. Batching small files would help
  over the WAN.

## Development

Set `RC_NO_CONFIRM=1` to skip the "transfer running, really quit?" prompt under
automation. The test suite sets it.

```sh
uv run pytest
```

The tests run the agent as a fake remote, over the real wire protocol, under every
interpreter available: Python 3.8 (`uv python install 3.8`), Python 2.7 (set
`RC_PY27=/path/to/python2.7`, e.g. a PyPy 2.7 build) and the current one (zstd).
They also check `agent.py` with vermin for 2.7/3.8 syntax and confirm it is pure ASCII.
