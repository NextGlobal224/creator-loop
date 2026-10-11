# Facebook local dependency provenance

- AutoCLI: https://github.com/nashsu/AutoCLI/tree/c0969e2c83b29a7528452b1ba555085deca8e00d ; facebook/search DOM approach adapted. Original first-anchor and query stripping do not prove actual post permalinks. Apache2.0 LICENSE/NOTICE retained; no AutoCLI.ai, cloud, extension/server, Meta API or service used.
- Playwright Python1.63.0: https://github.com/microsoft/playwright-python/tree/8cb967b3e4199bef5ce38e1cf34df1bf79cb8a8d ; Apache2.0. Windows x64 wheel SHA256 `2f9a707a6c6c91157ed77bff2b8caeb04b3c8d46e70d585fc134298cbe4b5cc6`. Dependencies pyee13.0.1, greenlet3.5.6 pinned. SDK/driver licenses travel in packaging.
- SDK browser revision1243, Chrome for Testing153.0.8010.12, official URL in `app/creator_loop/resources/facebook-browser-pin.json`. Per-file sizes/SHA256 are pinned (308files); browser is installed directly from upstream outside the public app ZIP. Google Chrome terms and Chromium third-party credits retained in `licenses/facebook`; Chrome executable is not declared wholly Apache/BSD. Browser execution is verified against that full pin with retained file leases.

No credentials/session/cookies, QA media/model/DB, build outputs or private logs belong in source or app artifact. Local browser profile lives only under user data. Browser verification/native DOM fixtures are not proof of real Facebook or final artifact acceptance.

Inspected AutoCLI adapter `adapters/facebook/search.yaml` SHA256 `6e60d751b7df7c1cc8c14a450b00b71f7110455f01944c9fddc0ce66e70f7ff3` at the pinned commit. This adapted DOM approach is independently filtered; no upstream cloud instructions are executed.
