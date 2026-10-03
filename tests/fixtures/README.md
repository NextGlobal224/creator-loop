# Synthetic media fixture

`video-red-blue.mp4` is a 1.8 KiB H.264 MP4 generated locally with PySide6 6.10.2 `QVideoFrameInput` and `QMediaRecorder`. It has 64×48 synthetic red frames followed by blue frames, about 0.97 seconds long. It contains no user media. SHA-256: `9bafe4454fbfe1d5a125afbd5453df3b675b7cecf56c44019503bd547855e34e`.

The Windows Qt Multimedia test decodes this committed file; the fixture generator and the encoder are not required at test time.

`video-with-tone.mp4` adds a 440 Hz synthetic AAC audio track to the same synthetic video. It is 6,953 bytes, generated locally with FFmpeg 9.0.2 for the audio Evidence tests. SHA-256: `ba6a7ddb7c1306a795dc996ebca656094f9098843c91f034ee5fdaaf1197a05c`. It contains no user media; FFmpeg is not required at test time.

`recovery-image.png` is a synthetic16x16 white RGB PNG generated using Python stdlib struct/zlib (IHDR8-bit RGB, scanline filter0, compressed IDAT, CRC32 chunks). It contains no user data. Startup recovery tests/probes compute its actual SHA256/byte size and verify the original remains unchanged; no image model/download is needed.
