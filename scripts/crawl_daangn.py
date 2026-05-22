#!/usr/bin/env python
import sys

from rentmap import main

if __name__ == "__main__":
    raise SystemExit(main(["crawl-daangn", *sys.argv[1:]]))
