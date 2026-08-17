#!/usr/bin/env python3
"""Read a local secret with visible masking on Windows consoles."""

from __future__ import annotations

import getpass
import os
import sys


def prompt_secret(prompt: str) -> str:
    if os.name != "nt":
        print("Input is hidden; paste the credential and press Enter.")
        return getpass.getpass(prompt)

    import msvcrt

    print("Paste the credential below. Each character appears as *; press Enter when done.")
    sys.stdout.write(prompt)
    sys.stdout.flush()
    value: list[str] = []
    while True:
        char = msvcrt.getwch()
        if char in {"\r", "\n"}:
            sys.stdout.write("\n")
            return "".join(value)
        if char == "\x03":
            sys.stdout.write("\n")
            raise KeyboardInterrupt
        if char in {"\x00", "\xe0"}:
            msvcrt.getwch()
            continue
        if char == "\b":
            if value:
                value.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if char.isprintable():
            value.append(char)
            sys.stdout.write("*")
            sys.stdout.flush()
