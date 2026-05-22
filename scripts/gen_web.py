#!/usr/bin/env python
import sys

from rentmap import main

if __name__ == "__main__":
    raise SystemExit(main(["gen-web", *sys.argv[1:]]))
