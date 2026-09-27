#!/usr/bin/env python3
"""Summarize saved stock-comparison observations without rerunning the desktop."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics


def percentile(values, fraction):
    """Nearest-rank percentile, not an interpolated synthetic observation."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(path):
    report = json.loads(path.read_text())
    groups = defaultdict(list)
    for run in report['runs']:
        groups[run['dataset'], run['variant']].append(run)
    result = []
    for (dataset, variant), runs in groups.items():
        samples = [s for r in runs for s in r['samples'] if not s.get('coldPrivateProcess', False)]
        entry = dict(dataset=dataset, variant=variant, runs=len(runs),
                     options=runs[0]['options'], samples=len(samples))
        if report['metadata']['mode'] == 'opening':
            for key in ['launchToRequestMs', 'launchToFrameMs', 'requestToFrameMs', 'completionToReturnMs']:
                values = [s[key] for s in samples]
                entry[key] = dict(mean=statistics.mean(values), median=statistics.median(values),
                                  p95=percentile(values, .95))
            entry['coldPrivateProcessFrameMs'] = [s['launchToFrameMs'] for r in runs
                                                 for s in r['samples'] if s['coldPrivateProcess']]
        else:
            for key in ['frameMs', 'handlerMs', 'cpuMs', 'guiInstructions']:
                values = [s[key] for s in samples]
                entry[key] = dict(mean=statistics.mean(values), median=statistics.median(values),
                                  p95=percentile(values, .95))
            entry['blockCpuMsPerInput'] = sum(r['measuredBlock']['cpuMs'] for r in runs) / len(samples)
            entry['blockCpuMsPerInputByPass'] = [r['measuredBlock']['cpuMs'] / len(r['samples']) for r in runs]
            entry['blockGuiInstructionsPerInput'] = sum(r['measuredBlock']['guiInstructions'] for r in runs) / len(samples)
            entry['framesOver16_7Ms'] = sum(s['frameMs'] > 16.7 for s in samples)
            entry['minCounterRunningFraction'] = min(s['counterRunningFraction'] for s in samples)
            entry['geometry'] = runs[0]['geometry']
        result.append(entry)
    return dict(source=str(path), metadata=report['metadata'], summary=result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', type=Path, nargs='+')
    args = parser.parse_args()
    print(json.dumps([summarize(path) for path in args.results], indent=2))
