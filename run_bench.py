#!/usr/bin/env python3
"""神衍 2×2 消融评测入口 (Windows + Docker DIY).

Usage:
    python run_bench.py
    python run_bench.py 1 --end 8
    python run_bench.py 1 --end 8 --sy1 --n-concurrent 4
    python run_bench.py --sy3 --redo
"""

from harness import main


if __name__ == "__main__":
    main()
