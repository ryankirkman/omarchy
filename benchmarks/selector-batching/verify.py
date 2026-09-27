#!/usr/bin/env python3
"""Check all stock ListModel roles and selection state before/after the tiny patch.

Uses the checkout's function and the actual Qt ListModel, in a private windowless
Quickshell process. Full window/input checks are in run.py.
"""
import argparse
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SOURCE = REPO/'shell/plugins/menu/Menu.qml'


def component(name, source):
    start = source.index('  function rebuildDmenuDisplay() {')
    end = source.index('  function rebuildDisplay() {', start)
    return '''
  component NAME: QtObject {
    id: root
    property ListModel displayModel: ListModel {}
    property bool searchDivider: true
    property string mode: "select"
    property string filterText: ""
    property var dmenuOptions: []
    property int layoutSerial: 0
    property int selectedIndex: 0
    function revealCursor() {}
    function snapshot() {
      var rows = []
      for (var i = 0; i < displayModel.count; i++) {
        var item = displayModel.get(i), row = {}
        for (var key of Object.keys(item).sort()) row[key] = item[key]
        rows.push(row)
      }
      return {rows:rows, selectedIndex:selectedIndex, layoutSerial:layoutSerial, searchDivider:searchDivider,
        values:rows.map(r => r.detail ? r.label + "\\t" + r.detail : r.label)}
    }
FUNCTION
  }
'''.replace('NAME', name).replace('FUNCTION', source[start:end])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, default=HERE/'results/filtering.json')
    args = parser.parse_args()
    saved = json.loads(args.results.read_text())
    datasets = []
    for name, options in saved['fixtures'].items():
        queries = sorted({s['query'] for r in saved['runs'] if r['dataset'] == name for s in r['samples']})
        datasets.append(dict(name=name, options=options, queries=queries))
    datasets.extend([
        dict(name='edge-cases', options=['', '\t', '\t\t', 'icon\t\tdetail', 'label',
            'icon\tLabel\tdetail\textra', 'icon\tDuplicate\tone', 'other\tDuplicate\ttwo',
            '<b>&"\tLiteral <b>&"\t$(literal)', '🙂\tCAFÉ İstanbul Σ 𐐀\tカフェ 😀',
            'label', None, 0, False],
            queries=['', ' ', 'label', 'detail', 'extra', 'DUPLICATE', '<b>', '$(literal)',
                     'café', 'İ', 'Σ', '𐐨', 'カフェ', '😀', '\t', 'zzzz']),
        dict(name='empty', options=[], queries=['', ' ', 'zzzz']),
        dict(name='replace-list', options=['replacement', 'icon\tnew\tdetail'], queries=['new', '', 'old']),
    ])
    with tempfile.TemporaryDirectory(prefix='verify-stock-batching-') as temporary:
        root = Path(temporary)
        patched = root/'patched-source.txt'
        patched.write_text(SOURCE.read_text())
        subprocess.run(['patch', '--silent', str(patched), str(HERE/'batch.patch')], check=True)
        (root/'Fixtures.js').write_text('var datasets = '+json.dumps(datasets)+';\n')
        qml = '''import QtQml
import QtQml.Models
import Quickshell
import "Fixtures.js" as Fixtures
Scope {
  Stock { id: stock }
  Batched { id: batched }
COMPONENTS
  Component.onCompleted: {
    var checks = 0, rowChecks = 0
    try {
      for (var data of Fixtures.datasets) for (var query of data.queries)
        for (var mode of ["select", "input"]) for (var index of [-1, 0, 10, 999]) {
          for (var picker of [stock, batched]) {
            picker.mode = mode; picker.dmenuOptions = data.options; picker.filterText = query
            picker.selectedIndex = index; picker.layoutSerial = 17; picker.searchDivider = true
            picker.rebuildDmenuDisplay()
          }
          var before = stock.snapshot(), after = batched.snapshot()
          if (JSON.stringify(before) !== JSON.stringify(after))
            throw new Error(data.name + " query=" + JSON.stringify(query) + " mode=" + mode + " index=" + index)
          checks++; rowChecks += before.rows.length
        }
      console.log("RESULT " + JSON.stringify({checks:checks, rowChecks:rowChecks, allRolesEqual:true,
        selectionValuesEqual:true, selectionClampEqual:true, layoutSerialEqual:true, inputModeEqual:true}))
    } catch (error) { console.log("FAILURE " + error) }
  }
}
'''.replace('COMPONENTS', component('Stock', SOURCE.read_text()) + component('Batched', patched.read_text()))
        (root/'shell.qml').write_text(qml)
        runtime = root/'runtime'
        runtime.mkdir(mode=0o700)
        process = subprocess.Popen(['quickshell', '--no-color', '-p', str(root/'shell.qml')],
            env=dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_QPA_PLATFORMTHEME='generic',
                     XDG_RUNTIME_DIR=str(runtime)), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)
        output = b''
        deadline = time.monotonic() + 20
        try:
            while time.monotonic() < deadline:
                if not select.select([process.stdout], [], [], 1)[0]:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                output += chunk
                for line in output.decode(errors='replace').splitlines():
                    if 'FAILURE ' in line:
                        raise RuntimeError(output.decode(errors='replace'))
                    if 'RESULT ' in line and output.endswith(b'\n'):
                        print(json.dumps(json.loads(line.split('RESULT ', 1)[1]), indent=2))
                        return
            raise RuntimeError(output.decode(errors='replace'))
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()


if __name__ == '__main__':
    main()
