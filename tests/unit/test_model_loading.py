"""Unit tests for the shared quiet/offline model-loading env defaults."""

from __future__ import annotations

import os
from unittest.mock import patch

from pipecat_context_hub.shared.model_loading import quiet_model_loading

_VARS = ("HF_HUB_OFFLINE", "HF_HUB_DISABLE_PROGRESS_BARS", "TRANSFORMERS_VERBOSITY")


class TestQuietModelLoading:
    def test_sets_defaults_when_unset(self):
        with patch.dict("os.environ", {}, clear=False):
            for var in _VARS:
                os.environ.pop(var, None)
            quiet_model_loading()
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
            assert os.environ["TRANSFORMERS_VERBOSITY"] == "error"

    def test_explicit_env_always_wins(self):
        explicit = {
            "HF_HUB_OFFLINE": "0",
            "HF_HUB_DISABLE_PROGRESS_BARS": "0",
            "TRANSFORMERS_VERBOSITY": "info",
        }
        with patch.dict("os.environ", explicit, clear=False):
            quiet_model_loading()
            for var, value in explicit.items():
                assert os.environ[var] == value
