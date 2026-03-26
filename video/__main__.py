"""Entry point for python -m video."""

import sys


def main():
    print("Usage: python -m video.<subcommand>")
    print()
    print("Available subcommands:")
    print("  video.generate              Generate video clips from scene prompts")
    print("  video.verify.runner         Verify generated clips meet acceptance criteria")
    print("  video.assemble.continuity   Check continuity across scenes")
    print("  video.assemble.stitch       Stitch clips into final render")
    sys.exit(0)


if __name__ == "__main__":
    main()
