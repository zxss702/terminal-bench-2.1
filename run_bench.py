#!/usr/bin/env python3
"""Terminal-Bench 2.1 multi-agent evaluation entry (Windows + Docker DIY).

Usage:
    python run_bench.py <start> [--end M] --logorythia --swe --auto --agentflow --claude
    python run_bench.py 1 --end 10 --logorythia --claude --redo
"""

from harness import main


if __name__ == "__main__":
    main()
