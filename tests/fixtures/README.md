All files in this directory are **SYNTHETIC** test inputs created by developers.
They contain no captured Xiaohongshu UI, no calibrated production control IDs,
and no real collected notes. They must never count toward real-device acceptance.

`synthetic_profile.toml` sets `synthetic = true`; production profile loading rejects it
even though `verified = true` permits parser contract tests. Real calibrated profiles
must be created separately using laboratory screenshots and UI trees.
