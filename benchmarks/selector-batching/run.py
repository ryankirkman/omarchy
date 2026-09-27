#!/usr/bin/env python3
"""Compare current Omarchy selectors before/after one batched model insertion.

Fresh private Quickshell test hosts with real Wayland windows. The stock source
tree and the installed desktop are never changed; batch.patch applies only to
temporary copies. Does not dispatch the selected shortcut action.
"""
import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PROMPTS = {'hyprland':'Keybindings', 'tmux':'Tmux keybindings', 'herdr':'Herdr keybindings'}
COMMANDS = {'hyprland':'omarchy-menu-keybindings', 'tmux':'omarchy-menu-tmux-keybindings', 'herdr':'omarchy-menu-herdr-keybindings'}
QUERIES = {'hyprland':['super','window','terminal','space super','splt wndw','zzzz',''],
           'tmux':['prefix','window','pane','copy','zzzz',''],
           'herdr':['navigate','workspace','prefix','window','zzzz','']}

def replace_once(source, old, new):
    if source.count(old) != 1:
        raise RuntimeError(f'Source changed: expected one {old!r}, got {source.count(old)}')
    return source.replace(old,new,1)

def instrument(source, variant, directory):
    stock = variant != 'custom'
    model, view, focus = ('displayModel','resultList','keyCatcher') if stock else ('results','list','query')
    ready = 'root.rowsLoaded && root.defaultMenuItems.length > 0 && !guardProc.running && !root.guardsPending' if stock else 'true'
    rows = f'''var rows = []
    for (var i = 0; i < {model}.count; i++) {{
      var r = {model}.get(i)
      rows.push({{label:r.label,detail:r.detail,value:r.detail ? r.label + "\\t" + r.detail : r.label}})
    }}
    return rows'''
    hook = f'''
  property var benchmarkClock: null
  property double benchmarkInputAt: 0
  property double benchmarkInputDone: 0
  property double benchmarkRequestAt: 0
  property var benchmarkSelection: null
  property int benchmarkCreated: 0
  property int benchmarkDestroyed: 0
  readonly property var benchmarkWindow: {focus}.Window.window
  readonly property bool benchmarkFocused: {focus}.activeFocus
  readonly property bool benchmarkReady: {ready}
  function benchmarkFocus() {{ {focus}.forceActiveFocus() }}
  function benchmarkCount() {{ return {model}.count }}
  function benchmarkRows() {{ {rows} }}
  function benchmarkGeometry() {{
    return {{cardWidth:card.width,cardHeight:card.height,listHeight:{view}.height,
      cacheBuffer:{view}.cacheBuffer,reuseItems:{view}.reuseItems}}
  }}
  function benchmarkCheck(expected, navigation) {{
    if (!navigation) return
    var row = {view}.itemAtIndex(root.selectedIndex)
    if (!row || !row.visible) throw new Error("Selected row is missing")
    if (row.y < {view}.contentY - 1 || row.y + row.height > {view}.contentY + {view}.height + 1)
      throw new Error("Selected row is outside viewport")
  }}
'''
    if not stock:
        hook += '''  function benchmarkCheckHighlights(expected) {
    for (var i = 0; i < expected.length; i++) {
      var row = list.itemAtIndex(i)
      if (row && (row.labelHtml !== expected[i].labelHtml || row.detailHtml !== expected[i].detailHtml))
        throw new Error("Visible underlines differ from oracle")
    }
  }
'''
    else:
        hook += '  function benchmarkCheckHighlights(expected) {}\n'
    source = replace_once(source,'  id: root\n','  id: root\n'+hook)
    source = replace_once(source,'  function open(payloadJson) {','  function open(payloadJson) {\n    benchmarkRequestAt = benchmarkClock.now()')
    signature = '  function finishRequest(selection) {' if stock else '  function finish(value) {'
    source = replace_once(source, signature, signature+'\n    benchmarkSelection = '+('selection' if stock else 'value'))
    source = replace_once(source,'Keys.onPressed: function(event) {', '''Keys.onPressed: function(event) {
            root.benchmarkInputAt = root.benchmarkClock.now()
            try {''')
    if stock:
        source = replace_once(source,'        }\n\n        ConfirmDialog {', '''            } finally { root.benchmarkInputDone = root.benchmarkClock.now() }
        }

        ConfirmDialog {''')
    else:
        source = replace_once(source,'            event.accepted = true\n          }','''            event.accepted = true
            } finally { root.benchmarkInputDone = root.benchmarkClock.now() }
          }''')
        source = replace_once(source,'onTextChanged: root.rebuild()',
            'onTextChanged: { root.rebuild(); if (root.benchmarkClock) root.benchmarkInputDone = root.benchmarkClock.now() }')
        source = replace_once(source,'path: runtime ? runtime + "/omarchy-shortcuts-" + display + ".sock" : ""',
            'path: '+json.dumps(str(directory / ('omarchy-shortcuts-'+display_name()+'.sock'))))
    source = replace_once(source,'              id: row\n' if stock else '            id: row\n',
        ('              id: row\n' if stock else '            id: row\n')+'''
              Component.onCompleted: root.benchmarkCreated++
              Component.onDestruction: root.benchmarkDestroyed++
''')
    return source

def display_name():
    value = Path(os.environ.get('WAYLAND_DISPLAY','default')).name
    return ''.join(c if c.isascii() and (c.isalnum() or c in '_.-') else '_' for c in value)

def actions_for(kind, mode):
    if mode == 'navigation':
        keys=[('Down',1,0x01000015)]*12 + [('PageDown',6,0x01000017)]*24 + [('Up',-1,0x01000013)]*12 + [('PageUp',-6,0x01000016)]*24
        return [dict(name=name,delta=delta,key=key) for name,delta,key in keys]
    actions,current=[],''
    for target in QUERIES[kind]:
        common=0
        while common < min(len(current),len(target)) and current[common]==target[common]: common+=1
        while len(current)>common:
            current=current[:-1]
            actions.append(dict(name='Backspace',key=0x01000003,query=current))
        for char in target[common:]:
            current+=char
            actions.append(dict(name='type',key=ord(char.upper()),text=char,query=current))
    return actions

def source_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def run_case(args, kind, options, variant, pass_index):
    with tempfile.TemporaryDirectory(prefix='omarchy-selector-bench-') as temporary:
        root=Path(temporary)
        for name in ['Commons','Ui','services']:
            (root/name).symlink_to(ROOT/'shell'/name,target_is_directory=True)
        (root/'Probe').mkdir()
        shutil.copy2(HERE/'build/libbenchmarkprobe.so',root/'Probe')
        (root/'Probe/qmldir').write_text(f'plugin benchmarkprobe {root}/Probe\nclassname ProbePlugin\n')
        source=(ROOT/'shell/plugins/menu/Menu.qml').read_text()
        if variant=='custom':
            source=(args.native_root/'plugin/Picker.qml').read_text()
            shutil.copytree(args.native_root/'plugin/Native',root/'Native')
            (root/'Native/qmldir').write_text(f'plugin shortcutsearch {root}/Native\nclassname ShortcutSearchPlugin\n')
        if variant=='stock-batched':
            patched=root/'unobserved.qml'
            patched.write_text(source)
            subprocess.run(['patch','--silent','--fuzz=0',str(patched),str(HERE/'batch.patch')],check=True)
            source=patched.read_text()
        (root/'Picker.qml').write_text(instrument(source,variant,root))
        shutil.copy2(ROOT/'shell/plugins/menu/MenuModel.js',root)
        shutil.copy2(HERE/'Oracle.js',root)
        config=dict(mode=args.mode,variant=variant,options=options,prompt=PROMPTS[kind],height=500,
            actions=actions_for(kind,args.mode),rounds=args.rounds,selectorOnly=False,
            screenshot='',captureQuery=args.capture_query)
        if args.screenshots:
            args.screenshots.mkdir(parents=True,exist_ok=True)
            config['screenshot']=str(args.screenshots.resolve()/f'{kind}-{variant}-pass{pass_index}.png')
        (root/'Fixture.js').write_text('var config = '+json.dumps(config)+';\n')
        shutil.copy2(HERE/'Harness.qml',root/'shell.qml')
        env=dict(os.environ,OMARCHY_PATH=str(ROOT),XDG_CACHE_HOME=str(root/'cache'),
            PATH=str(ROOT/'bin')+':'+os.environ['PATH'],QSG_INFO='1')
        process=subprocess.Popen(['taskset','-c',str(args.cpu),'quickshell','--no-color','-p',str(root/'shell.qml')],
            env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        pending,log,lines=b'',b'',deque()
        deadline=time.monotonic()+max(90,(args.rounds+1)*len(config['actions'])*.2+20)
        try:
            with selectors.DefaultSelector() as poller:
                poller.register(process.stdout,selectors.EVENT_READ)
                while time.monotonic()<deadline:
                    while lines:
                        line=lines.popleft()
                        if 'FAILURE ' in line: raise RuntimeError(line)
                        if 'RESULT ' in line:
                            result=json.loads(line.split('RESULT ',1)[1])
                            result.update(dataset=kind,options=len(options),passIndex=pass_index,
                                rendererLog=[s for s in log.decode(errors='replace').splitlines()
                                             if 'scenegraph' in s.lower() or 'rhi.' in s.lower()])
                            return result
                    if not poller.select(1): continue
                    chunk=os.read(process.stdout.fileno(),65536)
                    if not chunk: break
                    log+=chunk;pending+=chunk
                    while b'\n' in pending:
                        line,pending=pending.split(b'\n',1);lines.append(line.decode(errors='replace'))
            raise RuntimeError('Benchmark timed out or exited')
        except BaseException:
            print(log.decode(errors='replace'),file=__import__('sys').stderr)
            raise
        finally:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill();process.wait()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['typing','navigation'],default='typing')
    parser.add_argument('--dataset',choices=list(PROMPTS),action='append')
    parser.add_argument('--variant',choices=['stock','stock-batched','custom'],action='append')
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--passes',type=int,default=2)
    parser.add_argument('--cpu',type=int,default=0)
    parser.add_argument('--screenshots',type=Path)
    parser.add_argument('--capture-query',default='')
    parser.add_argument('--native-root',type=Path,help='Built contrib/shortcut-picker directory from the native draft')
    parser.add_argument('--fixtures',type=Path,help='Reuse the fixtures from a saved report')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if 'custom' in (args.variant or []) and not args.native_root: parser.error('--native-root is required for custom')
    if args.rounds<1 or args.passes<1: parser.error('rounds/passes must be positive')
    if not os.environ.get('WAYLAND_DISPLAY'): parser.error('A Wayland session is required')
    subprocess.run(['bash',str(HERE/'build-probe.sh')],check=True,stdout=subprocess.DEVNULL)
    datasets={}
    saved=json.loads(args.fixtures.read_text())['fixtures'] if args.fixtures else None
    for kind in args.dataset or list(PROMPTS):
        if saved is not None:
            datasets[kind]=saved[kind]
        else:
            output=subprocess.check_output([str(ROOT/'bin'/COMMANDS[kind]),'--print'],text=True,
                env=dict(os.environ,OMARCHY_PATH=str(ROOT),PATH=str(ROOT/'bin')+':'+os.environ['PATH']),timeout=20)
            datasets[kind]=output.splitlines()
        if not datasets[kind] or not all(isinstance(row,str) for row in datasets[kind]):
            raise RuntimeError('Empty or invalid dataset: '+kind)
    metadata=dict(packages=subprocess.check_output(['pacman','-Q','omarchy','quickshell','qt6-base','qt6-declarative'],text=True).splitlines(),
        sourceCommit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        stockMenuSha256=source_hash(ROOT/'shell/plugins/menu/Menu.qml'),batchPatchSha256=source_hash(HERE/'batch.patch'),
        mode=args.mode,cpu=args.cpu,rounds=args.rounds,passes=args.passes,queries=QUERIES,
        datasetHashes={k:hashlib.sha256(json.dumps(v).encode()).hexdigest() for k,v in datasets.items()})
    if args.native_root:
        metadata['nativeSourceHashes']={name:source_hash(args.native_root/name) for name in ['plugin/Picker.qml','native/search/search_model.cpp','native/search/search_model.h','native/selector.cpp']}
    report=dict(metadata=metadata,fixtures=datasets,runs=[])
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    for pass_index in range(args.passes):
        for kind,options in datasets.items():
            variants=args.variant or ['stock','stock-batched']
            for variant in variants if pass_index%2==0 else list(reversed(variants)):
                print(f'{args.mode}: {kind}, {variant}, pass {pass_index+1}',flush=True)
                report['runs'].append(run_case(args,kind,options,variant,pass_index))
                args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(args.output)

if __name__=='__main__': main()
