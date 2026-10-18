# Local video renderer component

The renderer invokes separately selected `ffmpeg-exe` and `ffprobe-exe` ENGINE
components. It starts no process and downloads nothing on import. Each render
verifies version, SHA-256, file identity, license declarations and local-use
permission, retains read-only component handles, and executes in a private
memory-bounded Windows Job with a finite deadline.

The local development component was obtained from the Windows build distributor
linked by the [FFmpeg download page](https://ffmpeg.org/download.html):
[Gyan release essentials 9.0.2](https://github.com/GyanD/codexffmpeg/releases/tag/9.0.2).
Gyan describes its static essentials builds as GPLv3. The supplied license is
preserved in `FFmpeg-GPL-3.0.txt`. No executable is committed or bundled here.
The build and license declarations are separate from Creator Loop's own source.

Pinned archive:

- Source: `https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip`
- Publisher checksum: `https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip.sha256`
- Archive SHA-256: `60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`
- `ffmpeg.exe` SHA-256: `3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec`
- `ffprobe.exe` SHA-256: `f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52`

An existing TrueType system font is used locally and fingerprinted in render
configuration. It is neither downloaded nor copied into the application package.
Vietnamese scene text and subtitles use UTF-8 text files with FFmpeg expansion
disabled; their text is never interpolated into command/filter syntax.

FFmpeg arguments follow the primary [filter documentation](https://ffmpeg.org/ffmpeg-filters.html)
and [ffprobe documentation](https://ffmpeg.org/ffprobe.html). Preview output uses
H.264/yuv420p video and AAC/stereo audio, bounded to 1080p/30 fps/180 seconds.
Each output is probed and fully decoded before a distinct MP4 filename is
published. Cancelled/failed partial files are removed, previous MP4 files remain.
