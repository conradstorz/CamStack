from __future__ import annotations
from pathlib import Path
from loguru import logger
import psutil, socket

RUNTIME = Path("/opt/camstack/runtime")
OVERLAY = RUNTIME / "overlay.ass"
VERSION = "2.0.0"

def get_first_ipv4() -> str:
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family == socket.AF_INET:
                ip = a.address
                if ip and not ip.startswith("127."):
                    return ip
    return "0.0.0.0"

def write_overlay(fallback: bool = False) -> Path:
    ip = get_first_ipv4()
    admin = f"https://{ip}/"
    tag = "(Fallback) " if fallback else ""

    text = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: HUD,Arial,24,&H00FFFFFF,&H000000FF,&H80000000,&H64000000,0,0,0,0,100,100,0,0,1,2,0,2,30,30,20,0\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,9:59:59.00,HUD,,0000,0000,0000,,{{{{\\an2}}}}{tag}CamStack v{VERSION} • Device IP: {ip} • {admin}\n"
    )

    OVERLAY.write_text(text, encoding="utf-8")
    logger.info(f"overlay written to {OVERLAY}")
    return OVERLAY

if __name__ == "__main__":
    write_overlay(False)
