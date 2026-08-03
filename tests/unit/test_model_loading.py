"""Unit tests for the shared quiet/offline model-loading env defaults."""

from __future__ import annotations

import os
from unittest.mock import patch

from pipecat_context_hub.shared.model_loading import quiet_model_loading

_VARS = ("HF_HUB_OFFLINE", "HF_HUB_DISABLE_PROGRESS_BARS")


class TestQuietModelLoading:
    def test_sets_defaults_when_unset(self):
        with patch.dict("os.environ", {}, clear=False):
            for var in _VARS:
                os.environ.pop(var, None)
            quiet_model_loading()
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"

    def test_explicit_env_always_wins(self):
        explicit = {
            "HF_HUB_OFFLINE": "0",
            "HF_HUB_DISABLE_PROGRESS_BARS": "0",
        }
        with patch.dict("os.environ", explicit, clear=False):
            quiet_model_loading()
            for var, value in explicit.items():
                assert os.environ[var] == value

    def test_does_not_set_transformers_verbosity(self):
        """The ONNX backend never imports transformers, so nothing reads it."""
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("TRANSFORMERS_VERBOSITY", None)
            quiet_model_loading()
            assert "TRANSFORMERS_VERBOSITY" not in os.environ
