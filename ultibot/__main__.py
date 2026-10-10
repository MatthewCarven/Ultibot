"""Ultibot CLI — Phase 0 + 1.

Usage:
    python -m ultibot validate <profile.json>     Check a profile against every law.
    python -m ultibot find <image> [options]      Is this template on screen right now?
    python -m ultibot shot <out.png> [options]    Save the current screen (or a region).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import validate_profile


# ---------------------------------------------------------------- helpers --

def _parse_region(text: str) -> tuple[int, int, int, int]:
    try:
        x, y, w, h = (int(v) for v in text.split(","))
        if w <= 0 or h <= 0:
            raise ValueError
        return x, y, w, h
    except ValueError:
        raise argparse.ArgumentTypeError("region must be x,y,w,h (e.g. 0,400,1920,680)") from None


def _parse_scales(text: str) -> tuple[float, ...]:
    try:
        scales = tuple(float(v) for v in text.split(","))
        if not scales or any(s <= 0 for s in scales):
            raise ValueError
        return scales
    except ValueError:
        raise argparse.ArgumentTypeError("scales must be positive numbers, e.g. 0.9,1.0,1.1")


def _resolve_assets(args: argparse.Namespace) -> Path:
    if args.assets:
        return Path(args.assets)
    if args.profile:
        return Path("assets") / args.profile
    raise SystemExit("find needs --assets <dir> or --profile <name> (uses assets/<name>)")


# --------------------------------------------------------------- validate --

def cmd_validate(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"ERROR  no such file: {path}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"ERROR  {path}: JSON syntax error: {exc}")
        return 1

    result = validate_profile(data)
    for warning in result.warnings:
        print(f"warn   {warning}")
    if not result.ok:
        for error in result.errors:
            print(f"ERROR  {error}")
        print(f"\n✗ {path.name} is INVALID ({len(result.errors)} error(s), "
              f"{len(result.warnings)} warning(s))")
        return 1

    groups = data.get("groups", {})
    n_nodes = len(data.get("tree", []))
    print(f"✓ {path.name} is valid — profile \"{data['profile']}\": "
          f"{len(groups)} group(s), {len(data.get('macros', {}))} macro(s), "
          f"{n_nodes} root node(s)"
          + (f", {len(result.warnings)} warning(s)" if result.warnings else ""))
    return 0


# -------------------------------------------------------------------- eyes --

def cmd_find(args: argparse.Namespace) -> int:
    import cv2  # deferred: validate stays stdlib-only

    from .eyes.capture import Screen
    from .eyes.match import ImageFinder

    try:
        finder = ImageFinder(_resolve_assets(args))
    except FileNotFoundError as exc:
        print(f"ERROR  {exc}")
        return 1

    if args.source:
        frame = cv2.imread(str(args.source), cv2.IMREAD_COLOR)
        if frame is None:
            print(f"ERROR  not a readable image: {args.source}")
            return 1
    else:
        try:
            frame = Screen(args.monitor).grab()
        except Exception as exc:  # mss ScreenShotError and friends
            print(f"ERROR  screen grab failed: {exc}")
            return 1

    started = time.perf_counter()
    try:
        match = finder.find_in_image(
            frame, args.image,
            confidence=args.confidence,
            region=args.region,
            grayscale=args.grayscale,
            scales=args.scales,
            variants=tuple(args.variants.split(",")) if args.variants else (),
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR  {exc}")
        return 1
    elapsed = (time.perf_counter() - started) * 1000

    print(f"{match}  [{elapsed:.1f} ms]")
    return 0 if match.found else 1


def cmd_shot(args: argparse.Namespace) -> int:
    import cv2

    from .eyes.capture import Screen

    try:
        frame = Screen(args.monitor).grab(region=args.region)
    except Exception as exc:
        print(f"ERROR  screen grab failed: {exc}")
        return 1
    cv2.imwrite(str(args.out), frame)
    h, w = frame.shape[:2]
    print(f"saved {args.out}  ({w}x{h})")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .brain.engine import Engine
    from .config import ProfileError, load_profile
    from .eyes.senses import LiveSenses

    try:
        profile = load_profile(args.profile)
    except ProfileError as exc:
        print(exc)
        return 1

    assets = Path(args.assets) if args.assets else Path("assets") / profile["profile"]
    try:
        senses = LiveSenses(assets, monitor=args.monitor)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR  {exc}")
        return 1

    engine = Engine(profile, senses)
    engine.run(ticks=args.ticks, seconds=args.seconds, verbose=args.verbose)
    return 0


# -------------------------------------------------------------------- main --

def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to cp1252 and choke on ✓/✗.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="ultibot",
        description="Ultibot — an AI game player built from boolean decision trees.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="check a profile against every law")
    p_validate.add_argument("profile", type=Path, help="path to the profile JSON")

    p_find = sub.add_parser("find", help="is a template image on screen right now?")
    p_find.add_argument("image", help="template filename inside the assets folder")
    p_find.add_argument("--profile", help="profile name (assets folder = assets/<name>)")
    p_find.add_argument("--assets", help="assets folder directly (overrides --profile)")
    p_find.add_argument("--confidence", "-c", type=float, default=0.8,
                        help="found threshold 0..1 (default: 0.8)")
    p_find.add_argument("--region", type=_parse_region, help="x,y,w,h to search only there")
    p_find.add_argument("--grayscale", "-g", action="store_true",
                        help="match in grayscale (survives hue shifts)")
    p_find.add_argument("--scales", type=_parse_scales, default=(1.0,),
                        help="comma-separated template scales, e.g. 0.9,1.0,1.1")
    p_find.add_argument("--variants", help="comma-separated extra template filenames")
    p_find.add_argument("--monitor", type=int, default=1, help="monitor index (default: 1 = primary)")
    p_find.add_argument("--in", dest="source", type=Path,
                        help="search this image file instead of the live screen")

    p_shot = sub.add_parser("shot", help="save the current screen (or a region) to a PNG")
    p_shot.add_argument("out", type=Path, help="output PNG path")
    p_shot.add_argument("--region", type=_parse_region, help="x,y,w,h to grab only there")
    p_shot.add_argument("--monitor", type=int, default=1, help="monitor index (default: 1 = primary)")

    p_run = sub.add_parser("run", help="run the engine (dry-run: logs, no input)")
    p_run.add_argument("profile", type=Path, help="path to the profile JSON")
    p_run.add_argument("--assets", help="assets folder (default: assets/<profile name>)")
    p_run.add_argument("--ticks", type=int, help="stop after N ticks")
    p_run.add_argument("--seconds", type=float, help="stop after N seconds")
    p_run.add_argument("--verbose", "-v", action="store_true",
                       help="also print skips and choose rolls")
    p_run.add_argument("--monitor", type=int, default=1, help="monitor index (default: 1 = primary)")

    args = parser.parse_args(argv)
    if args.command == "validate":
        return cmd_validate(args.profile)
    if args.command == "find":
        return cmd_find(args)
    if args.command == "shot":
        return cmd_shot(args)
    if args.command == "run":
        return cmd_run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
