"""Flippy's app window: history, setup and settings in one place, drawn by the system webview.

The page (flippy/shell/web) is plain HTML/CSS/JS shared by macOS and COSMIC. It talks to Python only through
Bridge (bridge.py), an allowlist of operations. Settings pages are generated from flippy/settings.py (schema.py),
so a new setting shows up without window work.
"""
