#!/usr/bin/env python3
"""Compare screenshot RGBA pixels without relying on PNG compression equality."""
import argparse
from array import array
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('before', type=Path)
parser.add_argument('after', type=Path)
args = parser.parse_args()

def dimensions(path):
    return list(map(int, subprocess.check_output(
        ['magick', 'identify', '-format', '%w %h', str(path)], text=True).split()))

size = dimensions(args.before)
if size != dimensions(args.after):
    raise SystemExit('Image dimensions differ')
pixels = [subprocess.check_output(['magick', str(path), '-depth', '8', 'rgba:-'])
          for path in [args.before, args.after]]
left, right = pixels
changed = [i for i, (x, y) in enumerate(zip(array('I', left), array('I', right))) if x != y]
print(json.dumps(dict(width=size[0], height=size[1], differentPixels=len(changed),
    totalPixels=len(left)//4, maxChannelDifference=max(
        (abs(left[4*i+c]-right[4*i+c]) for i in changed for c in range(4)), default=0)), indent=2))
