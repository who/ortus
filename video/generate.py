"""CLI entry point for video generation: python -m video.generate."""

import argparse
import sys
import time

from video.config import parse_model_config
from video.providers import get_provider


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate video clips from scene prompts",
    )
    parser.add_argument("--prompt", required=True, help="Generation prompt text")
    parser.add_argument("--duration", type=int, required=True, help="Duration in seconds")
    parser.add_argument("--output", required=True, help="Output file path")
    parser.add_argument("--config", default="MODEL.md", help="Path to MODEL.md config (default: MODEL.md)")
    args = parser.parse_args()

    try:
        config = parse_model_config(args.config)
    except (ValueError, FileNotFoundError) as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 1

    try:
        provider = get_provider(config)
    except ValueError as e:
        print(f"Provider error: {e}", file=sys.stderr)
        return 1

    resolution = config.get("resolution", "1280x720")
    poll_interval = config.get("poll_interval", 15)
    max_attempts = config.get("max_poll_attempts", 40)

    try:
        job_id = provider.submit(args.prompt, args.duration, resolution)
    except Exception as e:
        print(f"Submit failed: {e}", file=sys.stderr)
        return 1

    print(f"Job submitted: {job_id}")

    for attempt in range(1, max_attempts + 1):
        time.sleep(poll_interval)
        result = provider.poll(job_id)
        print(f"Poll attempt {attempt}/{max_attempts}: {result.status}")

        if result.status == "completed":
            try:
                provider.download(job_id, args.output)
            except Exception as e:
                print(f"Download failed: {e}", file=sys.stderr)
                return 1
            print(f"Video saved to {args.output}")
            return 0

        if result.status == "failed":
            print(f"Generation failed: {result.error or 'unknown error'}", file=sys.stderr)
            return 1

    print(f"Timed out after {max_attempts} poll attempts", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
