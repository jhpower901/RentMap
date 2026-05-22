#!/usr/bin/env python
import sys

from rentmap import main

if __name__ == "__main__":
    raise SystemExit(main(["crawl-dabang", *sys.argv[1:]]))
