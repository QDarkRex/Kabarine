import os

import yaml

DEFAULTS = {
    # Discord
    "prefix": "!",
    "activity_name": "TikTok",
    # Monitoring (paced scheduler)
    "sweep_target_seconds": 150,  # aim to re-check each account about this often
    "min_request_spacing": 1.5,   # safety floor: never fire TikTok requests faster than this
    "request_jitter": 0.5,        # +/- random seconds added to spacing (less robotic)
    "playlist_scan_count": 5,     # how many recent videos to scan per check
    # On-demand
    "auto_detect_links": True,       # auto-download TikTok/Instagram links pasted in chat
    "suppress_link_embeds": True,    # remove Discord's auto link-preview embed once the bot reposts it
    # Instagram (/ig)
    "download_timeout": 180,     # kill a gallery-dl download that runs longer than this (seconds)
    "max_files_per_post": 20,    # safety cap on media files pulled from one IG carousel
    # Instagram monitoring — MUCH slower than TikTok on purpose: Instagram flags
    # aggressive automated access, so we poll each account rarely to protect the burner.
    "ig_sweep_target_seconds": 900,   # per-burner: aim to re-check each of its accounts this often
    "ig_min_request_spacing": 20,     # never fire IG requests faster than this (seconds, per burner)
    "ig_request_jitter": 5,           # +/- random seconds added to IG spacing
    "ig_playlist_scan_count": 3,      # how many recent posts to scan per IG check
    # TikTok video quality
    "tiktok_native": True,       # grab highest resolution even if h265 (native, may not inline-preview);
                                 # set False to force h264 (always inline-plays, but can cap at 720p)
    # Upload handling
    "max_upload_mb": 10,         # Discord free-tier upload cap (~10 MB)
    "compress_oversize": True,   # ffmpeg-compress videos over the cap before falling back to a link
    "compress_timeout": 120,     # kill an ffmpeg compress that runs longer than this (seconds)
}


def load_config(path="configs.yml"):
    """Load configs.yml merged over DEFAULTS. Missing file -> all defaults."""
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        cfg.update({k: v for k, v in user.items() if v is not None})
    return cfg
