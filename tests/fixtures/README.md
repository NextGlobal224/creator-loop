# Synthetic media fixture

`video-red-blue.mp4` is a 1.8 KiB H.264 MP4 generated locally with PySide6 6.10.2 `QVideoFrameInput` and `QMediaRecorder`. It has 64×48 synthetic red frames followed by blue frames, about 0.97 seconds long. It contains no user media. SHA-256: `9bafe4454fbfe1d5a125afbd5453df3b675b7cecf56c44019503bd547855e34e`.

The Windows Qt Multimedia test decodes this committed file; the fixture generator and the encoder are not required at test time.
